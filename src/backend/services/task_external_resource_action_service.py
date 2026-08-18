"""Provider-neutral external resource actions for tasks.

Tasks may refer to canonical resources in systems outside Von.  This service
projects those references into a small, provider-neutral action collection for
the task UI, while leaving provider-specific lookup and URL construction next
to the corresponding integration code.

Only read-only ``open_resource`` navigation is supported here.  Effectful
actions (for example submit, approve, or reschedule) require their own
authority and effect contracts rather than being disguised as links.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

OPEN_RESOURCE_ACTION_KIND = "open_resource"

_ACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROVIDER_MODULE_PATHS = (
    "src.backend.integrations.google.gmail_task_external_resource_actions",
)


@dataclass(frozen=True)
class TaskExternalResourceActionError(RuntimeError):
    """Safe typed failure at the generic external-resource boundary."""

    reason_code: str
    safe_message: str

    def __str__(self) -> str:
        return self.safe_message


class TaskExternalResourceActionNotFoundError(TaskExternalResourceActionError):
    """The requested action is not available for this task."""


class TaskExternalResourceActionAccessError(TaskExternalResourceActionError):
    """The current actor may not resolve the requested external resource."""


class TaskExternalResourceActionResolutionError(TaskExternalResourceActionError):
    """The provider could not resolve the requested external resource."""


def _load_providers() -> tuple[ModuleType, ...]:
    """Load the deliberately small provider registry.

    Adding a provider is a registration change.  Provider-specific task
    interpretation, API access, and URL construction do not belong here.
    """

    providers: list[ModuleType] = []
    for path in _PROVIDER_MODULE_PATHS:
        try:
            providers.append(importlib.import_module(path))
        except Exception as exc:  # noqa: BLE001 - isolate optional providers
            logger.warning(
                "Task external-resource provider %s could not be loaded: %s",
                path,
                type(exc).__name__,
            )
    return tuple(providers)


def _normalise_optional_text(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length:
        return None
    return cleaned


def _normalise_action(raw_action: Any) -> dict[str, str] | None:
    if not isinstance(raw_action, Mapping):
        return None

    action_id = _normalise_optional_text(raw_action.get("action_id"), max_length=128)
    if action_id is None or _ACTION_ID_PATTERN.fullmatch(action_id) is None:
        return None

    kind = _normalise_optional_text(raw_action.get("kind"), max_length=64)
    if kind != OPEN_RESOURCE_ACTION_KIND:
        return None

    label = _normalise_optional_text(raw_action.get("label"), max_length=100)
    if label is None:
        return None

    action = {
        "action_id": action_id,
        "kind": kind,
        "label": label,
    }
    for key, max_length in (
        ("source_system", 64),
        ("resource_type", 64),
        ("description", 240),
    ):
        value = _normalise_optional_text(raw_action.get(key), max_length=max_length)
        if value is not None:
            action[key] = value
    return action


def _provider_actions(
    provider: ModuleType,
    task: Mapping[str, Any],
) -> list[dict[str, str]]:
    list_actions = getattr(provider, "list_actions", None)
    if not callable(list_actions):
        logger.error(
            "Task external-resource provider %s has no list_actions function",
            provider.__name__,
        )
        return []

    try:
        raw_actions = list_actions(task)
    except Exception as exc:  # noqa: BLE001 - isolate provider projection
        logger.warning(
            "Task external-resource provider %s could not list actions: %s",
            provider.__name__,
            type(exc).__name__,
        )
        return []

    if not isinstance(raw_actions, Sequence) or isinstance(
        raw_actions, (str, bytes, bytearray)
    ):
        logger.error(
            "Task external-resource provider %s returned a non-list action value",
            provider.__name__,
        )
        return []

    actions: list[dict[str, str]] = []
    for raw_action in raw_actions:
        action = _normalise_action(raw_action)
        if action is None:
            logger.warning(
                "Task external-resource provider %s returned an invalid action",
                provider.__name__,
            )
            continue
        actions.append(action)
    return actions


def list_task_external_resource_actions(
    task: Mapping[str, Any],
    *,
    providers: Sequence[ModuleType] | None = None,
) -> list[dict[str, str]]:
    """Return validated, provider-neutral navigation actions for one task."""

    selected_providers = (
        tuple(providers) if providers is not None else _load_providers()
    )
    actions: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for provider in selected_providers:
        for action in _provider_actions(provider, task):
            action_id = action["action_id"]
            if action_id in seen_ids:
                logger.warning(
                    "Ignoring duplicate task external-resource action id %s",
                    action_id,
                )
                continue
            seen_ids.add(action_id)
            actions.append(action)
    return actions


def _validate_resolved_url(value: Any) -> str:
    if not isinstance(value, str):
        raise TaskExternalResourceActionResolutionError(
            reason_code="external_resource_target_invalid",
            safe_message=(
                "The external resource did not resolve to a valid web address."
            ),
        )
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 4096:
        raise TaskExternalResourceActionResolutionError(
            reason_code="external_resource_target_invalid",
            safe_message=(
                "The external resource did not resolve to a valid web address."
            ),
        )
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TaskExternalResourceActionResolutionError(
            reason_code="external_resource_target_invalid",
            safe_message="The external resource did not resolve to a safe web address.",
        )
    return cleaned


def resolve_task_external_resource_action(
    task: Mapping[str, Any],
    action_id: str,
    *,
    providers: Sequence[ModuleType] | None = None,
) -> str:
    """Resolve one advertised task action to a safe HTTPS target."""

    cleaned_action_id = _normalise_optional_text(action_id, max_length=128)
    if (
        cleaned_action_id is None
        or _ACTION_ID_PATTERN.fullmatch(cleaned_action_id) is None
    ):
        raise TaskExternalResourceActionNotFoundError(
            reason_code="external_resource_action_not_found",
            safe_message="This external resource action is not available for the task.",
        )

    selected_providers = (
        tuple(providers) if providers is not None else _load_providers()
    )
    for provider in selected_providers:
        advertised_ids = {
            action["action_id"] for action in _provider_actions(provider, task)
        }
        if cleaned_action_id not in advertised_ids:
            continue

        resolve_action = getattr(provider, "resolve_action", None)
        if not callable(resolve_action):
            raise TaskExternalResourceActionResolutionError(
                reason_code="external_resource_provider_invalid",
                safe_message="The external resource provider is not available.",
            )
        try:
            target = resolve_action(task, cleaned_action_id)
        except (
            TaskExternalResourceActionAccessError,
            TaskExternalResourceActionResolutionError,
        ):
            raise
        except Exception as exc:
            logger.warning(
                "Task external-resource provider %s could not resolve action %s: %s",
                provider.__name__,
                cleaned_action_id,
                type(exc).__name__,
            )
            raise TaskExternalResourceActionResolutionError(
                reason_code="external_resource_resolution_failed",
                safe_message="The external resource could not be opened right now.",
            ) from exc

        if target is None:
            raise TaskExternalResourceActionResolutionError(
                reason_code="external_resource_resolution_failed",
                safe_message="The external resource could not be opened right now.",
            )
        return _validate_resolved_url(target)

    raise TaskExternalResourceActionNotFoundError(
        reason_code="external_resource_action_not_found",
        safe_message="This external resource action is not available for the task.",
    )


__all__ = [
    "OPEN_RESOURCE_ACTION_KIND",
    "TaskExternalResourceActionAccessError",
    "TaskExternalResourceActionError",
    "TaskExternalResourceActionNotFoundError",
    "TaskExternalResourceActionResolutionError",
    "list_task_external_resource_actions",
    "resolve_task_external_resource_action",
]
