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
        """Get existing context or create a new one."""
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
                    ctx.touch()
                    # Update user_id if provided and different
                    if user_id and ctx.user_id != user_id:
                        ctx.user_id = user_id
            else:
                ctx = WindowSessionContext(
                    window_session_id=window_session_id, user_id=user_id
                )
                self._sessions[window_session_id] = ctx
            return ctx

    def set(self, ctx: WindowSessionContext) -> None:
        """Store or update a window session context."""
        with self._lock:
            ctx.touch()
            self._sessions[ctx.window_session_id] = ctx

    def delete(self, window_session_id: str) -> bool:
        """Remove a window session. Returns True if it existed."""
        with self._lock:
            if window_session_id in self._sessions:
                del self._sessions[window_session_id]
                return True
            return False

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
    1. Window session context (if window_session_id provided and has org set)
    2. Flask session context (fallback for windows that don't use window sessions)

    Returns dict with: user_id, organisation_id, role, namespace, chat_session_id
    """
    # Check window session first
    if window_session_id:
        window_ctx = get_window_context(window_session_id)
        if window_ctx is not None:
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
