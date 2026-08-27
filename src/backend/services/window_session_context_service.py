"""
Window Session Context Service (JVNAUTOSCI-1011)

Manages per-window/tab session contexts to allow multiple browser windows
to have independent organisation contexts without interfering with each other.

The browser-wide Flask session cookie is used for authentication (user identity),
while window-specific context is cached in memory and its actor-owned
organisation selection is durably persisted for restart recovery.

This enables use cases like:
- Window A: #V#the_lu_witbrock_household
- Window B: #V#university_of_auckland_strong_ai_lab
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from .window_session_binding_store_service import (
    MongoWindowSessionBindingRepository,
    PersistedWindowSessionBinding,
    WindowSessionBindingOwnershipError,
    WindowSessionBindingRepository,
    WindowSessionBindingStoreUnavailable,
    WINDOW_SESSION_SCOPE_ORGANISATION,
    WINDOW_SESSION_SCOPE_PERSONAL,
)

logger = logging.getLogger(__name__)

# TTL for window sessions in seconds (1 hour)
WINDOW_SESSION_TTL_SECONDS = 3600

# Cleanup interval in seconds (every 5 minutes)
CLEANUP_INTERVAL_SECONDS = 300

# Refresh the durable sliding expiry at most once per cleanup interval.
DURABLE_REFRESH_INTERVAL_SECONDS = CLEANUP_INTERVAL_SECONDS


class WindowSessionOwnershipError(PermissionError):
    """Raised when a live window-session ID is used by a different actor."""


class WindowSessionContextUnavailable(PermissionError):
    """Raised when an actor-bound tab selector has no usable server binding."""


class WindowSessionContextRecoveryUnavailable(WindowSessionContextUnavailable):
    """Raised when an explicit selector cannot be recovered from durable state."""


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
    durable_scope_kind: Optional[str] = field(default=None, repr=False)
    durable_recovered: bool = field(default=False, repr=False)
    durable_refreshed_at: Optional[datetime] = field(default=None, repr=False)
    durably_persisted: bool = field(default=False, repr=False)

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
    Thread-safe in-memory L1 for window session contexts.

    An optional durable L2 stores only the actor-owned organisation selection.
    Roles and namespaces are always re-derived after recovery.
    """

    def __init__(
        self,
        binding_repository: WindowSessionBindingRepository | None = None,
    ) -> None:
        self._sessions: Dict[str, WindowSessionContext] = {}
        self._lock = threading.RLock()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._shutdown = False
        self._binding_repository = binding_repository

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

    @staticmethod
    def _context_from_binding(
        window_session_id: str,
        binding: PersistedWindowSessionBinding,
    ) -> WindowSessionContext:
        return WindowSessionContext(
            window_session_id=window_session_id,
            user_id=binding.user_id,
            organisation_concept_id=binding.organisation_concept_id,
            role_in_org=None,
            namespace=None,
            created_at=binding.created_at,
            last_accessed_at=datetime.now(timezone.utc),
            durable_scope_kind=binding.scope_kind,
            durable_recovered=True,
            durable_refreshed_at=binding.updated_at,
            durably_persisted=True,
        )

    def get_owned(
        self,
        window_session_id: str,
        user_id: Optional[str],
    ) -> Optional[WindowSessionContext]:
        """Return one exact actor-owned context from the shared binding.

        The durable row is deliberately consulted even when this process has a
        warm L1 entry.  Otherwise two web workers can keep different
        organisation selections for the same tab after a switch, or continue
        using a binding invalidated by a membership change.
        """

        requested_owner = _normalise_owner_id(user_id)
        if not requested_owner:
            return None
        ctx = self.get(window_session_id)
        repository = self._binding_repository
        if repository is None:
            if ctx is not None and _normalise_owner_id(ctx.user_id) == requested_owner:
                return ctx
            return None
        try:
            binding = repository.load_owned(window_session_id, requested_owner)
        except WindowSessionBindingStoreUnavailable as exc:
            raise WindowSessionContextRecoveryUnavailable(
                "window_session_context_recovery_unavailable"
            ) from exc
        if binding is None:
            # A shared deletion or expiry outranks this process's cache.  Do
            # not reveal or remove an entry owned by a different actor.
            with self._lock:
                cached = self._sessions.get(window_session_id)
                if (
                    cached is not None
                    and _normalise_owner_id(cached.user_id) == requested_owner
                    and not cached.durably_persisted
                    and not _has_authoritative_scope(cached)
                ):
                    # Chat-only legacy state is not an authority carrier and
                    # may still enrich a non-strict Flask compatibility read.
                    return cached
                if (
                    cached is not None
                    and _normalise_owner_id(cached.user_id) == requested_owner
                ):
                    del self._sessions[window_session_id]
            return None

        binding_org = _normalise_owner_id(binding.organisation_concept_id)
        cached_org = _normalise_owner_id(ctx.organisation_concept_id) if ctx else None
        cached_scope = ctx.durable_scope_kind if ctx else None
        cached_owner = _normalise_owner_id(ctx.user_id) if ctx else None
        if (
            ctx is not None
            and cached_owner == requested_owner
            and cached_scope == binding.scope_kind
            and cached_org == binding_org
        ):
            ctx.durably_persisted = True
            ctx.durable_refreshed_at = binding.updated_at
            self._refresh_durable_binding_if_due(ctx)
            return ctx

        recovered = self._context_from_binding(window_session_id, binding)
        if ctx is not None and cached_owner == requested_owner:
            # The conversation choice is process-local and is not an authority
            # carrier.  Preserve it while replacing stale organisation data.
            recovered.chat_session_id = ctx.chat_session_id
        # The exact actor-owned durable row outranks an unowned or other-actor
        # partial L1 entry. It never reveals that row to the other actor.
        with self._lock:
            self._sessions[window_session_id] = recovered
        return recovered

    def persist_authoritative_binding(
        self,
        ctx: WindowSessionContext,
        *,
        scope_kind: str,
    ) -> bool:
        """Persist an explicit Personal/organisation choice before L1 publish."""

        repository = self._binding_repository
        owner = _normalise_owner_id(ctx.user_id)
        if repository is None:
            return False
        if not owner:
            raise WindowSessionBindingStoreUnavailable(
                "authenticated window session owner is required"
            )
        try:
            binding = repository.save(
                window_session_id=ctx.window_session_id,
                user_id=owner,
                organisation_concept_id=ctx.organisation_concept_id,
                scope_kind=scope_kind,
            )
        except WindowSessionBindingOwnershipError as exc:
            raise WindowSessionOwnershipError(
                "window_session_owned_by_different_actor"
            ) from exc
        ctx.durable_scope_kind = binding.scope_kind
        ctx.durable_recovered = False
        ctx.durable_refreshed_at = binding.updated_at
        ctx.durably_persisted = True
        return True

    def _refresh_durable_binding_if_due(self, ctx: WindowSessionContext) -> None:
        repository = self._binding_repository
        owner = _normalise_owner_id(ctx.user_id)
        if repository is None or not owner or not ctx.durably_persisted:
            return
        refreshed_at = ctx.durable_refreshed_at
        if isinstance(refreshed_at, datetime):
            if refreshed_at.tzinfo is None:
                refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - refreshed_at).total_seconds()
            if age_seconds < DURABLE_REFRESH_INTERVAL_SECONDS:
                return
        try:
            refreshed = repository.touch_owned(ctx.window_session_id, owner)
        except WindowSessionBindingStoreUnavailable:
            logger.warning(
                "Unable to refresh durable window-session binding expiry",
                extra={"window_session_owner_present": True},
            )
            return
        if refreshed:
            ctx.durable_refreshed_at = datetime.now(timezone.utc)

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
        deleted_from_memory = False
        with self._lock:
            ctx = self._sessions.get(window_session_id)
            if ctx is not None and ctx.is_expired():
                del self._sessions[window_session_id]
                ctx = None
            stored_owner = _normalise_owner_id(ctx.user_id) if ctx else None
            requested_owner = _normalise_owner_id(user_id)
            if ctx is not None and stored_owner == requested_owner:
                del self._sessions[window_session_id]
                deleted_from_memory = True
        deleted_from_durable = False
        if self._binding_repository is not None and requested_owner:
            deleted_from_durable = self._binding_repository.delete_owned(
                window_session_id, requested_owner
            )
        return deleted_from_memory or deleted_from_durable

    def delete_all_owned(self, user_id: Optional[str]) -> int:
        """Delete every live window context owned by one exact actor."""

        requested_owner = _normalise_owner_id(user_id)
        if not requested_owner:
            return 0
        with self._lock:
            matching_ids = [
                window_session_id
                for window_session_id, ctx in self._sessions.items()
                if _normalise_owner_id(ctx.user_id) == requested_owner
            ]
            for window_session_id in matching_ids:
                del self._sessions[window_session_id]
        durable_count = 0
        if self._binding_repository is not None:
            durable_count = self._binding_repository.delete_all_owned(requested_owner)
        return max(len(matching_ids), durable_count)

    def delete_owned_for_organisation(
        self,
        user_id: Optional[str],
        organisation_concept_id: Optional[str],
    ) -> int:
        """Delete one actor's tabs bound to one organisation only."""

        requested_owner = _normalise_owner_id(user_id)
        requested_org = _normalise_owner_id(organisation_concept_id)
        if not requested_owner or not requested_org:
            return 0
        canonical_org = (
            requested_org
            if requested_org.startswith("#V#")
            else f"#V#{requested_org}"
        )
        with self._lock:
            matching_ids = [
                window_session_id
                for window_session_id, ctx in self._sessions.items()
                if _normalise_owner_id(ctx.user_id) == requested_owner
                and (
                    _normalise_owner_id(ctx.organisation_concept_id)
                    in {requested_org, canonical_org}
                )
            ]
            for window_session_id in matching_ids:
                del self._sessions[window_session_id]
        durable_count = 0
        if self._binding_repository is not None:
            durable_count = (
                self._binding_repository.delete_owned_for_organisation(
                    requested_owner,
                    canonical_org,
                )
            )
        return max(len(matching_ids), durable_count)

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
_window_session_binding_repository: WindowSessionBindingRepository | None = None


def get_window_session_binding_repository() -> WindowSessionBindingRepository:
    """Return the process repository used beneath the in-memory L1."""

    global _window_session_binding_repository
    if _window_session_binding_repository is None:
        _window_session_binding_repository = MongoWindowSessionBindingRepository(
            ttl_seconds=WINDOW_SESSION_TTL_SECONDS
        )
    return _window_session_binding_repository


def get_window_session_store() -> WindowSessionStore:
    """Get or create the global window session store."""
    global _window_session_store
    if _window_session_store is None:
        _window_session_store = WindowSessionStore(
            binding_repository=get_window_session_binding_repository()
        )
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
    current = store.get_or_create(window_session_id, user_id)
    clean_org_id = _normalise_owner_id(organisation_concept_id)
    canonical_org_id = (
        clean_org_id
        if clean_org_id is None or clean_org_id.startswith("#V#")
        else f"#V#{clean_org_id}"
    )
    updated = replace(
        current,
        organisation_concept_id=canonical_org_id,
        role_in_org=role_in_org,
        namespace=namespace,
        # Clear chat session when org changes (JVNAUTOSCI-1004 pattern).
        chat_session_id=None,
    )
    store.persist_authoritative_binding(
        updated,
        scope_kind=WINDOW_SESSION_SCOPE_ORGANISATION,
    )
    store.set(updated)
    return updated


def clear_window_organisation(
    window_session_id: str, namespace: str, user_id: Optional[str] = None
) -> WindowSessionContext:
    """Clear organisation context for a window session (switch to personal)."""
    store = get_window_session_store()
    current = store.get_or_create(window_session_id, user_id)
    updated = replace(
        current,
        organisation_concept_id=None,
        role_in_org=None,
        namespace=namespace,
        chat_session_id=None,
    )
    store.persist_authoritative_binding(
        updated,
        scope_kind=WINDOW_SESSION_SCOPE_PERSONAL,
    )
    store.set(updated)
    return updated


def _user_slug(user_id: str) -> str:
    cleaned = user_id[3:] if user_id.startswith("#V#") else user_id
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    if "+" in cleaned:
        cleaned = cleaned.split("+", 1)[0]
    import re

    return re.sub(r"[^a-z0-9]+", "_", cleaned.strip().lower()).strip("_")


def _recover_authoritative_scope(
    ctx: WindowSessionContext,
    *,
    user_id: str,
) -> WindowSessionContext | None:
    """Revalidate durable selection against current represented membership."""

    from .namespace_service import derive_namespace

    user_slug = _user_slug(user_id)
    if not user_slug:
        raise WindowSessionContextUnavailable("window_session_context_unavailable")
    if ctx.durable_scope_kind == WINDOW_SESSION_SCOPE_PERSONAL:
        recovered = replace(
            ctx,
            organisation_concept_id=None,
            role_in_org=None,
            namespace=derive_namespace(user_slug),
            durable_recovered=False,
        )
    else:
        org_id = _normalise_owner_id(ctx.organisation_concept_id)
        if not org_id:
            raise WindowSessionContextUnavailable(
                "window_session_context_unavailable"
            )
        canonical_org_id = org_id if org_id.startswith("#V#") else f"#V#{org_id}"
        canonical_user_id = user_id if user_id.startswith("#V#") else f"#V#{user_slug}"
        try:
            from .organisation_membership_service import (
                resolve_user_organisation_membership,
            )
            from .ontology_authority_membership_coordination_service import (
                organisation_membership_scope_barrier,
            )

            # Keep membership read and derived-scope publication atomic with
            # canonical role/membership mutations.  Without the shared barrier,
            # a recovery could recreate a binding between mutation invalidation
            # and the represented write.
            with organisation_membership_scope_barrier(
                canonical_user_id,
                canonical_org_id,
            ):
                membership = resolve_user_organisation_membership(
                    canonical_user_id,
                    canonical_org_id,
                )
                if isinstance(membership, dict):
                    role = str(membership.get("role") or "member").strip() or "member"
                    recovered = replace(
                        ctx,
                        organisation_concept_id=canonical_org_id,
                        role_in_org=role,
                        namespace=derive_namespace(user_slug, canonical_org_id[3:]),
                        durable_recovered=False,
                    )
                    get_window_session_store().set(recovered)
        except Exception as exc:
            if isinstance(exc, WindowSessionContextUnavailable):
                raise
            logger.warning(
                "Unable to revalidate recovered window organisation membership: %s",
                type(exc).__name__,
            )
            raise WindowSessionContextRecoveryUnavailable(
                "window_session_membership_recovery_unavailable"
            ) from exc
        if not isinstance(membership, dict):
            try:
                get_window_session_store().delete_if_owned(
                    ctx.window_session_id, user_id
                )
            except WindowSessionBindingStoreUnavailable:
                logger.warning(
                    "Unable to invalidate revoked durable window-session binding"
                )
            raise WindowSessionContextUnavailable(
                "window_session_context_unavailable"
            )
        return recovered
    get_window_session_store().set(recovered)
    return recovered


def get_effective_context(
    window_session_id: Optional[str],
    flask_session: dict,
    user_id: Optional[str],
    *,
    require_known_window: bool = False,
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
        try:
            window_ctx = get_window_session_store().get_owned(
                window_session_id, user_id
            )
        except WindowSessionContextRecoveryUnavailable:
            # Never let a durable-store outage fall through to another tab's
            # browser-wide organisation scope.
            raise
        if window_ctx is not None and window_ctx.durable_recovered:
            window_ctx = _recover_authoritative_scope(
                window_ctx,
                user_id=_normalise_owner_id(user_id) or "",
            )
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

            if require_known_window:
                raise WindowSessionContextUnavailable(
                    "window_session_context_unavailable"
                )

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

    if window_session_id and require_known_window:
        # Missing, expired and other-actor window selectors are intentionally
        # indistinguishable. Falling through to the browser-wide Flask org
        # could execute an authorised action in the wrong tab's organisation.
        raise WindowSessionContextUnavailable("window_session_context_unavailable")

    # Fall back to Flask session only for compatibility callers that did not
    # present a window selector (or explicitly opted into non-strict lookup).
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
    return get_window_session_store().delete_if_owned(
        window_session_id.strip(), user_id
    )


def delete_all_window_contexts_owned_by(user_id: Optional[str]) -> int:
    """Invalidate all derived per-window role caches for one actor."""

    return get_window_session_store().delete_all_owned(user_id)


def delete_window_contexts_owned_by_user_for_organisation(
    user_id: Optional[str],
    organisation_concept_id: Optional[str],
) -> int:
    """Invalidate derived tab authority for one exact actor/org pair."""

    return get_window_session_store().delete_owned_for_organisation(
        user_id,
        organisation_concept_id,
    )


def set_window_chat_session(
    window_session_id: str,
    chat_session_id: Optional[str],
    user_id: Optional[str] = None,
) -> WindowSessionContext:
    """Set the active chat session for a window."""
    store = get_window_session_store()
    ctx = store.get_or_create(window_session_id, user_id)
    updated = replace(ctx, chat_session_id=chat_session_id)
    store.set(updated)
    return updated
