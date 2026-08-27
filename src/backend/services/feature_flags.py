import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}

_EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS: ContextVar[tuple[str, ...]] = ContextVar(
    "event_workflow_launch_suppression_reasons",
    default=(),
)


def current_event_workflow_launch_suppression_reason() -> str | None:
    """Return the innermost request-local event-launch suppression reason."""

    reasons = _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.get()
    return reasons[-1] if reasons else None


def event_workflow_launches_suppressed() -> bool:
    """Return whether event-driven workflow launch is suppressed in this context."""

    return bool(_EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.get())


@contextmanager
def suppress_event_workflow_launches(reason: str) -> Iterator[None]:
    """Suppress event-workflow fan-out within one nested execution context."""

    cleaned_reason = str(reason or "").strip() or "unspecified"
    current_reasons = _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.get()
    token = _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.set(
        (*current_reasons, cleaned_reason)
    )
    try:
        yield
    finally:
        _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.reset(token)


def _read_env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalised = str(raw).strip().lower()
    if normalised in _TRUTHY:
        return True
    if normalised in _FALSY:
        return False
    return default


def get_expert_tabs_enabled() -> bool:
    """Return whether expert tabs should be enabled (safe default: disabled)."""
    return _read_env_flag("VON_EXPERT_TABS_ENABLED", default=False)


def get_expert_footer_enabled() -> bool:
    """Return whether expert footer status (PID/RAG) should be enabled."""
    return _read_env_flag("VON_EXPERT_FOOTER_ENABLED", default=False)


def get_durable_workflows_enabled(*, default: bool = False) -> bool:
    """Return whether the durable workflow worker/scheduler should run.

    Safe default is disabled unless explicitly enabled via
    ``VON_DURABLE_WORKFLOWS_ENABLE=1``.
    """

    return _read_env_flag("VON_DURABLE_WORKFLOWS_ENABLE", default=default)


def get_event_workflow_integration_enabled(*, default: bool = True) -> bool:
    """Return whether event -> workflow integration is enabled."""

    if event_workflow_launches_suppressed():
        return False
    return _read_env_flag("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", default=default)


def get_workflow_discovery_cache_invalidation_enabled(
    *,
    default: bool = True,
) -> bool:
    """Return whether mutation paths should invalidate workflow discovery caches.

    Event-workflow-owned mutations may suppress their own recursive launch and
    routing-cache fan-out through a request-local context. Import lazily to
    avoid coupling ordinary feature-flag reads to workflow service start-up.
    """

    if event_workflow_launches_suppressed():
        return False

    return _read_env_flag(
        "VON_WORKFLOW_DISCOVERY_CACHE_INVALIDATION_ENABLE",
        default=default,
    )


def get_display_elements_screen_fence_compat_enabled(*, default: bool = True) -> bool:
    """Return whether legacy screen-fence backfill compatibility remains enabled.

    JVNAUTOSCI-1149 introduces a structured ``display_elements`` contract. This
    flag gates the older pathway that mutates screen text to append required JSON
    fences directly, so deployments can migrate safely.
    """

    return _read_env_flag(
        "VON_DISPLAY_ELEMENTS_SCREEN_FENCE_COMPAT_ENABLE",
        default=default,
    )
