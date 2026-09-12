"""Validate task execution tokens without choosing a model or provider policy.

This is a narrow input contract, not a catalogue of available models/efforts.
Codex advertises reasoning levels dynamically; preserve exact future/provider
tokens. Reject the demonstrated task-field mix-ups, never infer a replacement.
"""

from __future__ import annotations


def normalise_task_execution_preference(
    value: object, *, field_name: str
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    value = value.strip()
    if not value:
        return None

    if field_name == "requested_model":
        # Exact observed display labels, not a model-family allowlist or a
        # lexical guess about arbitrary future model IDs.
        if (
            value.casefold() in {"codexdgx", "astra"}
            or value.startswith("#V#")
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError(
                "requested_model requires an exact model ID (for example "
                "gpt-6-astra), not an agent concept ID or display label. Put the "
                "coding agent in assignee_concept_id; supply the user's exact "
                "model ID, or omit/clear requested_model to inherit the worker "
                "default only when no model was requested."
            )
    elif field_name == "requested_reasoning_effort":
        if value.casefold() in {"extra_high", "extra-high", "extra high"}:
            raise ValueError(
                "requested_reasoning_effort requires the exact level xhigh for "
                "extra-high reasoning. Retry with xhigh to preserve that choice; "
                "do not drop it or substitute high/the worker default."
            )
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError(
                "requested_reasoning_effort requires one exact model-supported "
                "reasoning token (for example high or xhigh)."
            )
    else:
        raise ValueError(f"Unknown task execution preference: {field_name}")
    return value
