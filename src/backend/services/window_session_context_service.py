"""
Window Session Context Service (JVNAUTOSCI-1011)

Manages per-window/tab session contexts to allow multiple browser windows
to have independent organisation contexts without interfering with each other.

The browser-wide Flask session cookie is used for authentication (user identity),
while window-specific context (organisation, namespace, role) is stored in
an in-memory dict keyed by window_session_id.

This enables use cases like:
- Window A: #V#the_lu_witbrock_household
- Window B: #V#university_of_auckland_strong_ai_lab
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# TTL for window sessions in seconds (1 hour)
WINDOW_SESSION_TTL_SECONDS = 3600

# Cleanup interval in seconds (every 5 minutes)
CLEANUP_INTERVAL_SECONDS = 300


class WindowSessionOwnershipError(PermissionError):
    """Raised when a live window-session ID is used by a different actor."""


def _normalise_owner_id(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _has_authoritative_scope(ctx: "WindowSessionContext") -> bool:
    return bool(
        _normalise_owner_id(ctx.organisation_concept_id)
        or _normalise_owner_id(ctx.role_in_org)
        or _normalise_owner_id(ctx.namespace)
    )


@dataclass
class WindowSessionContext:
    """Context data for a single window/tab session."""

    window_session_id: str
    user_id: Optional[str] = None
    organisation_concept_id: Optional[str] = None
    role_in_org: Optional[str] = None
    namespace: Optional[str] = None
    chat_session_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def touch(self) -> None:
        """Update last accessed timestamp."""
        self.last_accessed_at = datetime.now(timezone.utc)

    def is_expired(self, ttl_seconds: int = WINDOW_SESSION_TTL_SECONDS) -> bool:
        """Check if this session has expired."""
        age_seconds = (
            datetime.now(timezone.utc) - self.last_accessed_at
        ).total_seconds()
        return age_seconds > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to dict for API responses."""
        return {
            "window_session_id": self.window_session_id,
            "user_id": self.user_id,
            "organisation_id": self.organisation_concept_id,
            "role": self.role_in_org,
            "namespace": self.namespace,
            "chat_session_id": self.chat_session_id,
        }


class WindowSessionStore:
    """
    Thread-safe in-memory store for window session contexts.

    In production, this could be backed by Redis or a similar store
    for multi-process/multi-server deployments.
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, WindowSessionContext] = {}
        self._lock = threading.RLock()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._shutdown = False

    def start_cleanup_thread(self) -> None:
        """Start background thread that periodically cleans up expired sessions."""
        if self._cleanup_thread is not None:
            return
        self._shutdown = False
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="WindowSessionCleanup"
        )
        self._cleanup_thread.start()
        logger.info("Window session cleanup thread started")

    def stop_cleanup_thread(self) -> None:
        """Signal cleanup thread to stop."""
        self._shutdown = True

    def _cleanup_loop(self) -> None:
        """Background loop that removes expired sessions."""
        while not self._shutdown:
            try:
                self.cleanup_expired()
            except Exception as e:
                logger.warning(f"Error during window session cleanup: {e}")
            # Sleep in short intervals to allow quick shutdown
            for _ in range(int(CLEANUP_INTERVAL_SECONDS)):
                if self._shutdown:
                    break
                time.sleep(1)

    def get(self, window_session_id: str) -> Optional[WindowSessionContext]:
        """Get a window session context by ID, updating last accessed time."""
        with self._lock:
            ctx = self._sessions.get(window_session_id)
            if ctx is not None:
                if ctx.is_expired():
                    # Remove expired session
                    del self._sessions[window_session_id]
                    return None
                ctx.touch()
            return ctx

    def get_or_create(
        self, window_session_id: str, user_id: Optional[str] = None
    ) -> WindowSessionContext:
        """Get or create a context without rebinding a live ID across actors."""
        with self._lock:
            ctx = self._sessions.get(window_session_id)
            if ctx is not None:
                if ctx.is_expired():
                    # Replace expired session
                    ctx = WindowSessionContext(
                        window_session_id=window_session_id, user_id=user_id
                    )
                    self._sessions[window_session_id] = ctx
                else:
                    stored_owner = _normalise_owner_id(ctx.user_id)
                    requested_owner = _normalise_owner_id(user_id)
                    if stored_owner and stored_owner != requested_owner:
                        raise WindowSessionOwnershipError(
                            "window_session_owned_by_different_actor"
                        )
                    if not stored_owner and requested_owner:
                        # An unowned partial/chat-only entry may be claimed at
                        # login. Never let a caller adopt pre-existing org,
                        # role, or namespace authority from an anonymous ID.
                        if _has_authoritative_scope(ctx):
                            raise WindowSessionOwnershipError(
                                "window_session_unowned_authority_conflict"
                            )
                        ctx.user_id = requested_owner
                    ctx.touch()
            else:
                ctx = WindowSessionContext(
                    window_session_id=window_session_id, user_id=user_id
                )
                self._sessions[window_session_id] = ctx
            return ctx

    def set(self, ctx: WindowSessionContext) -> None:
        """Store a context without replacing another live actor's entry."""
        with self._lock:
            existing = self._sessions.get(ctx.window_session_id)
            if existing is not None and not existing.is_expired():
                stored_owner = _normalise_owner_id(existing.user_id)
                incoming_owner = _normalise_owner_id(ctx.user_id)
                if stored_owner and stored_owner != incoming_owner:
                    raise WindowSessionOwnershipError(
                        "window_session_owned_by_different_actor"
                    )
                if (
                    not stored_owner
                    and incoming_owner
                    and _has_authoritative_scope(existing)
                ):
                    raise WindowSessionOwnershipError(
                        "window_session_unowned_authority_conflict"
                    )
            ctx.touch()
            self._sessions[ctx.window_session_id] = ctx

    def delete(self, window_session_id: str) -> bool:
        """Remove a window session. Returns True if it existed."""
        with self._lock:
            if window_session_id in self._sessions:
                del self._sessions[window_session_id]
                return True
            return False

    def delete_if_owned(
        self,
        window_session_id: str,
        user_id: Optional[str],
    ) -> bool:
        """Delete a live context only when the authenticated owner matches."""
        with self._lock:
            ctx = self._sessions.get(window_session_id)
            if ctx is None:
                return False
            if ctx.is_expired():
                del self._sessions[window_session_id]
                return False
            stored_owner = _normalise_owner_id(ctx.user_id)
            requested_owner = _normalise_owner_id(user_id)
            if not stored_owner or stored_owner != requested_owner:
                return False
            del self._sessions[window_session_id]
            return True

    def cleanup_expired(self) -> int:
        """Remove all expired sessions. Returns count removed."""
        with self._lock:
            expired_ids = [
                sid for sid, ctx in self._sessions.items() if ctx.is_expired()
            ]
            for sid in expired_ids:
                del self._sessions[sid]
            if expired_ids:
                logger.debug(f"Cleaned up {len(expired_ids)} expired window sessions")
            return len(expired_ids)

    def count(self) -> int:
        """Return number of active sessions."""
        with self._lock:
            return len(self._sessions)


# Global singleton store
_window_session_store: Optional[WindowSessionStore] = None


def get_window_session_store() -> WindowSessionStore:
    """Get or create the global window session store."""
    global _window_session_store
    if _window_session_store is None:
        _window_session_store = WindowSessionStore()
        _window_session_store.start_cleanup_thread()
    return _window_session_store


def generate_window_session_id() -> str:
    """Generate a new unique window session ID."""
    return f"ws_{uuid4().hex}"


# Convenience functions for route handlers


def get_window_context(
    window_session_id: Optional[str],
) -> Optional[WindowSessionContext]:
    """Get window context if a valid window_session_id is provided."""
    if not window_session_id:
        return None
    return get_window_session_store().get(window_session_id)


def get_or_create_window_context(
    window_session_id: str, user_id: Optional[str] = None
) -> WindowSessionContext:
    """Get or create window context for the given window_session_id."""
    return get_window_session_store().get_or_create(window_session_id, user_id)


def set_window_organisation(
    window_session_id: str,
    organisation_concept_id: Optional[str],
    role_in_org: Optional[str],
    namespace: str,
    user_id: Optional[str] = None,
) -> WindowSessionContext:
    """Set organisation context for a window session."""
    store = get_window_session_store()
    ctx = store.get_or_create(window_session_id, user_id)
    ctx.organisation_concept_id = organisation_concept_id
    ctx.role_in_org = role_in_org
    ctx.namespace = namespace
    # Clear chat session when org changes (JVNAUTOSCI-1004 pattern)
    ctx.chat_session_id = None
    store.set(ctx)
    return ctx


def clear_window_organisation(
    window_session_id: str, namespace: str, user_id: Optional[str] = None
) -> WindowSessionContext:
    """Clear organisation context for a window session (switch to personal)."""
    store = get_window_session_store()
    ctx = store.get_or_create(window_session_id, user_id)
    ctx.organisation_concept_id = None
    ctx.role_in_org = None
    ctx.namespace = namespace
    ctx.chat_session_id = None
    store.set(ctx)
    return ctx


def get_effective_context(
    window_session_id: Optional[str],
    flask_session: dict,
    user_id: Optional[str],
) -> Dict[str, Any]:
    """
    Get the effective context combining window session (if present) and Flask session.

    Priority:
    1. Window session context when it has authoritative scope information
       (organisation/role/namespace)
    2. Flask session context when the window entry is partial (for example, it
       only tracks a chat session id and would otherwise erase org scope)
    3. Bare window session context if no richer scope exists anywhere

    Returns dict with: user_id, organisation_id, role, namespace, chat_session_id
    """
    def _window_context_has_authoritative_scope(ctx: WindowSessionContext) -> bool:
        return bool(
            (
                isinstance(ctx.organisation_concept_id, str)
                and ctx.organisation_concept_id.strip()
            )
            or (isinstance(ctx.role_in_org, str) and ctx.role_in_org.strip())
            or (isinstance(ctx.namespace, str) and ctx.namespace.strip())
        )

    # Check window session first
    if window_session_id:
        window_ctx = get_window_context(window_session_id)
        if window_ctx is not None:
            stored_owner = _normalise_owner_id(window_ctx.user_id)
            authenticated_owner = _normalise_owner_id(user_id)
            if not stored_owner or stored_owner != authenticated_owner:
                # Treat an unknown ID and another actor's ID identically. In
                # particular, do not expose its org, role, namespace, chat ID,
                # or even a distinguishable context source.
                window_ctx = None

        if window_ctx is not None:
            if _window_context_has_authoritative_scope(window_ctx):
                return {
                    "user_id": user_id,
                    "organisation_id": window_ctx.organisation_concept_id,
                    "role": window_ctx.role_in_org,
                    "namespace": window_ctx.namespace,
                    "chat_session_id": window_ctx.chat_session_id,
                    "source": "window_session",
                }

            flask_org = flask_session.get("organisation_concept_id")
            flask_role = flask_session.get("role_in_org")
            flask_namespace = flask_session.get("namespace")
            if flask_org or flask_role or flask_namespace:
                return {
                    "user_id": user_id,
                    "organisation_id": flask_org,
                    "role": flask_role,
                    "namespace": flask_namespace,
                    "chat_session_id": window_ctx.chat_session_id
                    or flask_session.get("session_id"),
                    "source": "flask_session",
                }

            return {
                "user_id": user_id,
                "organisation_id": window_ctx.organisation_concept_id,
                "role": window_ctx.role_in_org,
                "namespace": window_ctx.namespace,
                "chat_session_id": window_ctx.chat_session_id,
                "source": "window_session",
            }

    # Fall back to Flask session
    return {
        "user_id": user_id,
        "organisation_id": flask_session.get("organisation_concept_id"),
        "role": flask_session.get("role_in_org"),
        "namespace": flask_session.get("namespace"),
        "chat_session_id": flask_session.get("session_id"),
        "source": "flask_session",
    }


def delete_window_context_if_owned(
    window_session_id: Optional[str],
    user_id: Optional[str],
) -> bool:
    """Invalidate the current actor's per-window context, if one exists."""
    if not isinstance(window_session_id, str) or not window_session_id.strip():
        return False
    return get_window_session_store().delete_if_owned(window_session_id.strip(), user_id)


def set_window_chat_session(
    window_session_id: str,
    chat_session_id: Optional[str],
    user_id: Optional[str] = None,
) -> WindowSessionContext:
    """Set the active chat session for a window."""
    store = get_window_session_store()
    ctx = store.get_or_create(window_session_id, user_id)
    ctx.chat_session_id = chat_session_id
    store.set(ctx)
    return ctx
