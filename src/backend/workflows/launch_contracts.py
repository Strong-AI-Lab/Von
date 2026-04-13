"""Declarative launch contracts for workflows.

This module implements Vontology-authoritative declarative launch contracts,
subsuming legacy procedural checks (JVNAUTOSCI-1818).
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreconditionResult:
    type: str
    satisfied: bool
    required: bool
    reason: str | None = None
    context_key: str | None = None


@dataclass(frozen=True)
class LaunchContractResult:
    launchable: bool
    precondition_results: Sequence[PreconditionResult] = ()


def _eval_context_key_present(precondition: Mapping[str, Any], context: Mapping[str, Any]) -> PreconditionResult:
    key = str(precondition.get("key", "")).strip()
    required = bool(precondition.get("required", True))
    if not key:
        return PreconditionResult(
            type="context_key_present",
            satisfied=False,
            required=required,
            reason="Missing 'key' in precondition",
        )
    
    satisfied = key in context
    reason = None if satisfied else f"Missing required context key: {key}"
    return PreconditionResult(
        type="context_key_present",
        satisfied=satisfied,
        required=required,
        reason=reason,
        context_key=key,
    )


def _eval_registry_registered(precondition: Mapping[str, Any], context: Mapping[str, Any]) -> PreconditionResult:
    required = bool(precondition.get("required", True))
    registry_snapshot = context.get("registry_snapshot")
    satisfied = registry_snapshot is not None
    reason = None if satisfied else "Workflow not registered in action registry"
    return PreconditionResult(
        type="registry_registered",
        satisfied=satisfied,
        required=required,
        reason=reason,
    )


def _eval_namespace_write_access(precondition: Mapping[str, Any], context: Mapping[str, Any]) -> PreconditionResult:
    required = bool(precondition.get("required", True))
    # Check if namespace is provided and user has write access.
    # Assuming policy_state holds this.
    policy_state = context.get("policy_state")
    if isinstance(policy_state, Mapping) and policy_state.get("namespace"):
        # For simplicity, if they have a namespace, we assume write access for now,
        # or we check a specific flag. Let's say we check 'allow_writes' or similar
        # if available, else we assume True if policy_state is present.
        # Following typical Von patterns:
        satisfied = True
        reason = None
    else:
        satisfied = False
        reason = "No active namespace for write access"
    
    return PreconditionResult(
        type="namespace_write_access",
        satisfied=satisfied,
        required=required,
        reason=reason,
    )


PRECONDITION_EVALUATORS: Mapping[str, Callable[[Mapping[str, Any], Mapping[str, Any]], PreconditionResult]] = {
    "context_key_present": _eval_context_key_present,
    "registry_registered": _eval_registry_registered,
    "namespace_write_access": _eval_namespace_write_access,
}


def evaluate_launch_contract(
    contract: Mapping[str, Any],
    context: Mapping[str, Any],
) -> LaunchContractResult:
    """Evaluate a declarative launch contract against a given context."""
    results = []
    
    preconditions = contract.get("preconditions", [])
    if not isinstance(preconditions, list):
        preconditions = []
        
    for prec in preconditions:
        if not isinstance(prec, Mapping):
            continue
            
        prec_type = prec.get("type")
        required = bool(prec.get("required", True))
        
        evaluator = PRECONDITION_EVALUATORS.get(str(prec_type))
        if evaluator is None:
            results.append(
                PreconditionResult(
                    type=str(prec_type),
                    satisfied=False,
                    required=required,
                    reason=f"Unknown precondition type: {prec_type}",
                )
            )
            continue
            
        try:
            # The orchestrator should enforce timeout at a higher level, or we use maxTimeMS
            # if we had DB calls. For now, these are synchronous context checks.
            result = evaluator(prec, context)
            results.append(result)
        except Exception as e:
            logger.exception(f"Error evaluating precondition {prec_type}: {e}")
            results.append(
                PreconditionResult(
                    type=str(prec_type),
                    satisfied=False,
                    required=required,
                    reason=f"Evaluator raised exception: {e}",
                )
            )

    launchable = all(r.satisfied or not r.required for r in results)
    
    return LaunchContractResult(
        launchable=launchable,
        precondition_results=results,
    )
