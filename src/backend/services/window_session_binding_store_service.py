"""Durable, actor-bound organisation selections for browser window sessions.

The window-session identifier is only a selector.  Authentication and current
organisation membership remain the authority for every recovered scope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pymongo.errors import DuplicateKeyError, PyMongoError

from ..db.mongo_client import get_window_session_binding_collection

WINDOW_SESSION_BINDING_SCHEMA_VERSION = "window_session_binding.v1"
WINDOW_SESSION_SCOPE_ORGANISATION = "organisation"
WINDOW_SESSION_SCOPE_PERSONAL = "personal"


class WindowSessionBindingStoreError(RuntimeError):
    """Base class for durable window-binding failures."""


class WindowSessionBindingStoreUnavailable(WindowSessionBindingStoreError):
    """Raised when a binding cannot be read or written durably."""


class WindowSessionBindingOwnershipError(WindowSessionBindingStoreError):
    """Raised when an existing live selector belongs to another actor."""


@dataclass(frozen=True)
class PersistedWindowSessionBinding:
    """The minimum durable selection needed to reconstruct a trusted scope."""

    window_session_key: str
    user_id: str
    scope_kind: str
    organisation_concept_id: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class WindowSessionBindingRepository(Protocol):
    """Persistence seam used by the in-memory window-session cache."""

    def load_owned(
        self, window_session_id: str, user_id: str
    ) -> PersistedWindowSessionBinding | None: ...

    def load_for_mutation(
        self, window_session_id: str
    ) -> PersistedWindowSessionBinding | None: ...

    def save(
        self,
        *,
        window_session_id: str,
        user_id: str,
        organisation_concept_id: str | None,
        scope_kind: str,
    ) -> PersistedWindowSessionBinding: ...

    def touch_owned(self, window_session_id: str, user_id: str) -> bool: ...

    def delete_owned(self, window_session_id: str, user_id: str) -> bool: ...

    def delete_owned_for_organisation(
        self, user_id: str, organisation_concept_id: str
    ) -> int: ...

    def delete_all_owned(self, user_id: str) -> int: ...


def _clean_required(value: str, *, field: str) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _window_session_key(window_session_id: str) -> str:
    """Return a content-free stable key; never persist the browser selector."""

    cleaned = _clean_required(window_session_id, field="window_session_id")
    return "sha256:" + hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _normalise_org_id(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


def _coerce_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class MongoWindowSessionBindingRepository:
    """Mongo-backed L2 for the small per-window organisation selection."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        collection_getter: Callable[[], object | None] = (
            get_window_session_binding_collection
        ),
    ) -> None:
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._collection_getter = collection_getter

    def _collection(self):
        try:
            collection = self._collection_getter()
        except Exception as exc:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding store is unavailable"
            ) from exc
        if collection is None:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding store is unavailable"
            )
        return collection

    @staticmethod
    def _from_document(document: object) -> PersistedWindowSessionBinding | None:
        if not isinstance(document, dict):
            return None
        key = document.get("_id")
        user_id = document.get("user_id")
        scope_kind = document.get("scope_kind")
        created_at = _coerce_utc_datetime(document.get("created_at"))
        updated_at = _coerce_utc_datetime(document.get("updated_at"))
        expires_at = _coerce_utc_datetime(document.get("expires_at"))
        if not all(
            (
                isinstance(key, str) and key,
                isinstance(user_id, str) and user_id,
                scope_kind
                in {
                    WINDOW_SESSION_SCOPE_ORGANISATION,
                    WINDOW_SESSION_SCOPE_PERSONAL,
                },
                created_at is not None,
                updated_at is not None,
                expires_at is not None,
            )
        ):
            return None
        organisation_concept_id = _normalise_org_id(
            document.get("organisation_concept_id")
            if isinstance(document.get("organisation_concept_id"), str)
            else None
        )
        if (
            scope_kind == WINDOW_SESSION_SCOPE_ORGANISATION
            and organisation_concept_id is None
        ):
            return None
        if scope_kind == WINDOW_SESSION_SCOPE_PERSONAL:
            organisation_concept_id = None
        return PersistedWindowSessionBinding(
            window_session_key=key,
            user_id=user_id,
            scope_kind=scope_kind,
            organisation_concept_id=organisation_concept_id,
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
        )

    def _load(self, query: dict[str, object]) -> PersistedWindowSessionBinding | None:
        now = datetime.now(UTC)
        try:
            document = self._collection().find_one(
                {**query, "expires_at": {"$gt": now}}
            )
        except PyMongoError as exc:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding read failed"
            ) from exc
        return self._from_document(document)

    def load_owned(
        self, window_session_id: str, user_id: str
    ) -> PersistedWindowSessionBinding | None:
        return self._load(
            {
                "_id": _window_session_key(window_session_id),
                "user_id": _clean_required(user_id, field="user_id"),
            }
        )

    def load_for_mutation(
        self, window_session_id: str
    ) -> PersistedWindowSessionBinding | None:
        return self._load({"_id": _window_session_key(window_session_id)})

    def save(
        self,
        *,
        window_session_id: str,
        user_id: str,
        organisation_concept_id: str | None,
        scope_kind: str,
    ) -> PersistedWindowSessionBinding:
        clean_user_id = _clean_required(user_id, field="user_id")
        if scope_kind not in {
            WINDOW_SESSION_SCOPE_ORGANISATION,
            WINDOW_SESSION_SCOPE_PERSONAL,
        }:
            raise ValueError("scope_kind must be organisation or personal")
        clean_org_id = _normalise_org_id(organisation_concept_id)
        if scope_kind == WINDOW_SESSION_SCOPE_ORGANISATION and not clean_org_id:
            raise ValueError("organisation_concept_id is required")
        if scope_kind == WINDOW_SESSION_SCOPE_PERSONAL:
            clean_org_id = None

        key = _window_session_key(window_session_id)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        collection = self._collection()
        try:
            # An expired selector may be reclaimed, matching the L1 semantics.
            collection.delete_one({"_id": key, "expires_at": {"$lte": now}})
            result = collection.update_one(
                {"_id": key, "user_id": clean_user_id},
                {
                    "$setOnInsert": {
                        "schema_version": WINDOW_SESSION_BINDING_SCHEMA_VERSION,
                        "created_at": now,
                    },
                    "$set": {
                        "user_id": clean_user_id,
                        "scope_kind": scope_kind,
                        "organisation_concept_id": clean_org_id,
                        "updated_at": now,
                        "expires_at": expires_at,
                    },
                },
                upsert=True,
            )
        except DuplicateKeyError as exc:
            raise WindowSessionBindingOwnershipError(
                "window_session_owned_by_different_actor"
            ) from exc
        except PyMongoError as exc:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding write failed"
            ) from exc
        if not getattr(result, "acknowledged", True):
            raise WindowSessionBindingStoreUnavailable(
                "window session binding write was not acknowledged"
            )
        binding = self.load_owned(window_session_id, clean_user_id)
        if binding is None:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding write could not be read back"
            )
        return binding

    def touch_owned(self, window_session_id: str, user_id: str) -> bool:
        now = datetime.now(UTC)
        try:
            result = self._collection().update_one(
                {
                    "_id": _window_session_key(window_session_id),
                    "user_id": _clean_required(user_id, field="user_id"),
                    "expires_at": {"$gt": now},
                },
                {
                    "$set": {
                        "updated_at": now,
                        "expires_at": now + timedelta(seconds=self._ttl_seconds),
                    }
                },
            )
        except PyMongoError as exc:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding refresh failed"
            ) from exc
        return bool(getattr(result, "matched_count", 0))

    def delete_owned(self, window_session_id: str, user_id: str) -> bool:
        try:
            result = self._collection().delete_one(
                {
                    "_id": _window_session_key(window_session_id),
                    "user_id": _clean_required(user_id, field="user_id"),
                }
            )
        except PyMongoError as exc:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding deletion failed"
            ) from exc
        return bool(getattr(result, "deleted_count", 0))

    def delete_all_owned(self, user_id: str) -> int:
        try:
            result = self._collection().delete_many(
                {"user_id": _clean_required(user_id, field="user_id")}
            )
        except PyMongoError as exc:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding deletion failed"
            ) from exc
        return int(getattr(result, "deleted_count", 0) or 0)

    def delete_owned_for_organisation(
        self, user_id: str, organisation_concept_id: str
    ) -> int:
        clean_org_id = _normalise_org_id(organisation_concept_id)
        if not clean_org_id:
            raise ValueError("organisation_concept_id is required")
        try:
            result = self._collection().delete_many(
                {
                    "user_id": _clean_required(user_id, field="user_id"),
                    "scope_kind": WINDOW_SESSION_SCOPE_ORGANISATION,
                    "organisation_concept_id": clean_org_id,
                }
            )
        except PyMongoError as exc:
            raise WindowSessionBindingStoreUnavailable(
                "window session binding deletion failed"
            ) from exc
        return int(getattr(result, "deleted_count", 0) or 0)
