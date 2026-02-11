import os


_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}


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

    return _read_env_flag("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", default=default)
