from __future__ import annotations

from typing import Any, Mapping

_ACTIONLESS_PRE_ACTION_CONTRACT_KEYS: tuple[str, ...] = (
    "preconditions",
    "reads_variables",
    "reads_context_keys",
)


def has_actionless_pre_action_contract(
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Return whether an actionless state still has a meaningful runtime gate.

    Output and mapping metadata are enforced after an action (or subworkflow)
    runs, so they do not by themselves make an actionless state executable.
    """

    if not isinstance(metadata, Mapping):
        return False
    return any(bool(metadata.get(key)) for key in _ACTIONLESS_PRE_ACTION_CONTRACT_KEYS)

