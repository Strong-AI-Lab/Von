"""Local-only browser-test authentication and fixture support.

JVNAUTOSCI-1747

Provides a deliberately gated local-development helper for establishing a real
authenticated Flask session for a dedicated pseudouser, then seeding
representative user-view fixture data for browser acceptance testing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
from typing import Any, Iterable, Optional

from flask import current_app, has_request_context, request, session

from ..db.mongo_client import get_concepts_collection
from ..security.access_control import bypass_access_control
from ..services import chat_history_service
from ..services.concept_service import (
    ConceptNotFoundError,
    create_concept,
    get_concept_by_concept_id,
)
from ..services.message_service import (
    MESSAGE_STATUS_SENT,
    MESSAGE_TYPE_CONCEPT_ID,
    create_message,
)
from ..services.namespace_service import derive_namespace
from ..services.organisation_membership_service import create_organisation_membership
from ..services.settings_service import (
    MultipleUsersForEmailError,
    _find_user_concept_by_email,
)
from ..services.text_value_service import upsert_text_for_concept
from ..services.window_session_context_service import (
    set_window_chat_session,
    set_window_organisation,
)

_BROWSER_TEST_FIXTURE_ID = "browser_user_view.v1"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_LOOPBACK_REMOTES = {"127.0.0.1", "::1", "localhost", ""}


@dataclass(frozen=True)
class BrowserTestAuthConfig:
    enabled: bool
    pseudouser_name: str
    pseudouser_email: str
    pseudouser_concept_id: str
    organisation_concept_id: str


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return cleaned or "browser_test_user"


def _ensure_concept_id(value: str, *, fallback_from: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = f"#V#{_slugify(fallback_from)}"
    if not raw.startswith("#V#"):
        raw = f"#V#{raw}"
    return raw


def get_browser_test_auth_config() -> BrowserTestAuthConfig:
    pseudouser_name = (
        str(os.getenv("VON_BROWSER_TEST_PSEUDOUSER_NAME") or "").strip()
        or "Zhan von Witbrock"
    )
    pseudouser_email = (
        str(os.getenv("VON_BROWSER_TEST_PSEUDOUSER_EMAIL") or "").strip()
        or "zhanvonwitbrock@gmail.com"
    )
    pseudouser_concept_id = _ensure_concept_id(
        str(os.getenv("VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID") or "").strip(),
        fallback_from=pseudouser_name,
    )
    organisation_concept_id = _ensure_concept_id(
        str(os.getenv("VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID") or "").strip(),
        fallback_from="university_of_auckland_strong_ai_lab",
    )
    return BrowserTestAuthConfig(
        enabled=_truthy_env("VON_BROWSER_TEST_AUTH_ENABLED", default=False),
        pseudouser_name=pseudouser_name,
        pseudouser_email=pseudouser_email,
        pseudouser_concept_id=pseudouser_concept_id,
        organisation_concept_id=organisation_concept_id,
    )


def browser_test_auth_allowed_for_request() -> tuple[bool, str | None]:
    config = get_browser_test_auth_config()
    if not config.enabled:
        return False, "Browser-test auth is disabled"

    if not has_request_context():
        return False, "Browser-test auth requires a request context"

    if getattr(current_app, "testing", False):
        return True, None

    host = str(getattr(request, "host", "") or "").split(":", 1)[0].strip("[]").lower()
    remote_addr = str(getattr(request, "remote_addr", "") or "").strip("[]").lower()
    forwarded_host = str(request.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip().split(":", 1)[0].strip("[]").lower()
    if host not in _LOOPBACK_HOSTS:
        return False, "Browser-test auth is available only on localhost"
    if forwarded_host and forwarded_host not in _LOOPBACK_HOSTS:
        return False, "Browser-test auth rejects forwarded non-local hosts"
    if remote_addr not in _LOOPBACK_REMOTES:
        return False, "Browser-test auth is available only from the local machine"
    return True, None


def describe_browser_test_mode() -> dict[str, Any]:
    config = get_browser_test_auth_config()
    available, reason = browser_test_auth_allowed_for_request()
    payload: dict[str, Any] = {
        "configured": config.enabled,
        "available": available,
        "reason": None if available else reason,
    }
    if config.enabled:
        payload.update(
            {
                "display_name": config.pseudouser_name,
                "email": config.pseudouser_email,
                "user_concept_id": config.pseudouser_concept_id,
                "organisation_concept_id": config.organisation_concept_id,
                "fixture_id": _BROWSER_TEST_FIXTURE_ID,
            }
        )
    return payload


def _get_concept_name(doc: Optional[dict[str, Any]], fallback: str) -> str:
    if isinstance(doc, dict):
        for key in ("direct_concept_name", "name"):
            value = doc.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _ensure_person_concept(
    *,
    concept_id: str,
    name: str,
    email: str,
) -> dict[str, Any]:
    with bypass_access_control():
        try:
            existing_email_concept = _find_user_concept_by_email(email)
        except MultipleUsersForEmailError:
            raise

        if isinstance(existing_email_concept, dict) and existing_email_concept.get(
            "concept_id"
        ):
            concept_doc = existing_email_concept
        else:
            try:
                concept_doc = get_concept_by_concept_id(concept_id)
            except ConceptNotFoundError:
                concept_doc = create_concept(
                    name=name,
                    concept_id=concept_id,
                    parent_concept_ids=["#V#person"],
                )
            if not isinstance(concept_doc, dict):
                raise RuntimeError(
                    f"Browser-test concept lookup failed for {concept_id}"
                )

        resolved_concept_id = str(concept_doc.get("concept_id") or concept_id).strip()
        if not resolved_concept_id:
            raise RuntimeError("Browser-test user resolution produced no concept_id")

        upsert_text_for_concept(
            subject_concept_id=resolved_concept_id,
            predicate="#V#has_email",
            text=email,
            provenance={"source": "browser_test_fixture"},
        )

        try:
            refreshed = get_concept_by_concept_id(resolved_concept_id)
            if isinstance(refreshed, dict):
                return refreshed
        except Exception:
            pass
        return concept_doc


def _ensure_org_exists(organisation_concept_id: str) -> dict[str, Any]:
    with bypass_access_control():
        try:
            concept_doc = get_concept_by_concept_id(organisation_concept_id)
        except ConceptNotFoundError as exc:
            raise ValueError(
                f"Browser-test organisation {organisation_concept_id} does not exist"
            ) from exc
        if not isinstance(concept_doc, dict):
            raise ValueError(
                f"Browser-test organisation {organisation_concept_id} could not be resolved"
            )
        return concept_doc


def _ensure_org_membership(
    *,
    user_concept_id: str,
    organisation_concept_id: str,
    role: str,
) -> None:
    with bypass_access_control():
        create_organisation_membership(
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            role=role,
        )


def _current_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_fixture_specs(
    *,
    pseudouser_concept_id: str,
    reviewer_concept_id: str,
    scout_concept_id: str,
    organisation_concept_id: str,
) -> Iterable[dict[str, Any]]:
    return (
        {
            "key": "workflow-review-request",
            "sender_id": reviewer_concept_id,
            "recipient_ids": [pseudouser_concept_id],
            "subject": "Workflow preview needs a sanity check",
            "content": (
                "Could you review the self-authored workflow preview before lunch? "
                "The long rendered evidence cartouche in the Messages pane is the "
                "kind of thing we want clipped-layout testing to keep handling well."
            ),
            "metadata": {
                "intent": "review_request",
                "delivery_channel": "interuser_message",
            },
            "mark_unread_for": pseudouser_concept_id,
            "org_id": organisation_concept_id,
        },
        {
            "key": "workflow-review-reply",
            "sender_id": pseudouser_concept_id,
            "recipient_ids": [reviewer_concept_id],
            "subject": "Re: Workflow preview needs a sanity check",
            "content": (
                "I can do that. Please keep the long provenance cartouche and the "
                "narrow-width Messages layout in the acceptance pass so we can "
                "catch clipping regressions directly in browser view."
            ),
            "metadata": {
                "intent": "review_response",
                "delivery_channel": "interuser_message",
            },
            "org_id": organisation_concept_id,
        },
        {
            "key": "paper-digest",
            "sender_id": scout_concept_id,
            "recipient_ids": [pseudouser_concept_id],
            "subject": "Fresh papers worth skimming",
            "content": (
                "Three new papers landed overnight. The second one looks especially "
                "relevant to neuro-symbolic planning, and the summary block is long "
                "enough to exercise message preview wrapping."
            ),
            "metadata": {
                "intent": "paper_digest",
                "delivery_channel": "interuser_message",
            },
            "mark_unread_for": pseudouser_concept_id,
            "org_id": organisation_concept_id,
        },
    )


def _chat_fixture_specs() -> Iterable[dict[str, Any]]:
    return (
        {
            "session_id": "browser-fixture-messages-acceptance",
            "session_name": "Messages acceptance and clipped-layout regression",
            "history": (
                {
                    "fixture_entry_id": "messages-acceptance-user-1",
                    "role": "user",
                    "content": (
                        "Please inspect the Messages interface at desktop and mobile "
                        "widths and make sure long cartouches do not clip."
                    ),
                },
                {
                    "fixture_entry_id": "messages-acceptance-assistant-1",
                    "role": "assistant",
                    "content": (
                        "I checked the Messages pane at 1280px and 390px widths. "
                        "Long message previews, badges, and cartouches now wrap "
                        "without forcing the conversation pane off-screen."
                    ),
                },
            ),
        },
        {
            "session_id": "browser-fixture-user-view-state",
            "session_name": "Authenticated user-view fixture sanity check",
            "history": (
                {
                    "fixture_entry_id": "user-view-fixture-user-1",
                    "role": "user",
                    "content": (
                        "Set up the representative browser fixture state with saved "
                        "conversations, unread messages, and realistic organisation "
                        "context so user-view testing is repeatable."
                    ),
                },
                {
                    "fixture_entry_id": "user-view-fixture-assistant-1",
                    "role": "assistant",
                    "content": (
                        "Done. The fixture now includes direct messages, an "
                        "organisation-scoped namespace, and saved sessions that are "
                        "safe to refresh repeatedly while staying useful for DevTools "
                        "acceptance checks."
                    ),
                },
            ),
        },
    )


def _ensure_fixture_message(
    *,
    spec: dict[str, Any],
) -> dict[str, Any]:
    coll = get_concepts_collection()
    if coll is None:
        raise RuntimeError("Database not available for browser-test messages")

    key = spec["key"]
    query = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        "concept_data.metadata.browser_test_fixture_id": _BROWSER_TEST_FIXTURE_ID,
        "concept_data.metadata.browser_test_message_key": key,
    }
    with bypass_access_control():
        existing = coll.find_one(query, {"concept_id": 1, "concept_data.read_by": 1})
    if isinstance(existing, dict) and existing.get("concept_id"):
        reset_payload: dict[str, Any] = {
            "updated_at": datetime.now(timezone.utc),
            "concept_data.subject": spec.get("subject"),
            "concept_data.content_fallback": spec.get("content"),
            "concept_data.message_status": MESSAGE_STATUS_SENT,
            "concept_data.deleted": False,
        }
        if spec.get("mark_unread_for"):
            reset_payload["concept_data.read_by"] = []
        with bypass_access_control():
            coll.update_one(
                {"concept_id": existing["concept_id"]},
                {"$set": reset_payload},
            )
            upsert_text_for_concept(
                subject_concept_id=existing["concept_id"],
                predicate="hasDescription",
                text=str(spec.get("content") or ""),
                lang="en-NZ",
                provenance={"source": "browser_test_fixture"},
            )
        return {"concept_id": existing["concept_id"], "created": False}

    metadata = dict(spec.get("metadata") or {})
    metadata.update(
        {
            "browser_test_fixture_id": _BROWSER_TEST_FIXTURE_ID,
            "browser_test_message_key": key,
            "browser_test_seeded_at": _current_iso(),
        }
    )
    created = create_message(
        sender_id=spec["sender_id"],
        recipient_ids=list(spec["recipient_ids"]),
        content=str(spec["content"]),
        subject=spec.get("subject"),
        org_id=spec.get("org_id"),
        metadata=metadata,
    )
    return {"concept_id": created.get("concept_id"), "created": True}


def _ensure_fixture_chat_session(
    *,
    user_concept_id: str,
    namespace: str,
    organisation_concept_id: str,
    role_in_org: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    session_id = str(spec["session_id"])
    session_name = str(spec["session_name"])

    chat_history_service.create_chat_session(
        user_id=user_concept_id,
        session_id=session_id,
        session_name=session_name,
        namespace=namespace,
        organisation_concept_id=organisation_concept_id,
        role_in_org=role_in_org,
    )

    coll = chat_history_service.get_chat_history_collection_service()
    if coll is None:
        raise RuntimeError("Chat history collection unavailable for browser-test fixture")

    with bypass_access_control():
        doc = coll.find_one(
            {"user_id": user_concept_id, "session_id": session_id},
            {"history.fixture_entry_id": 1},
        )

    existing_ids: set[str] = set()
    if isinstance(doc, dict):
        for entry in doc.get("history") or []:
            fixture_entry_id = entry.get("fixture_entry_id") if isinstance(entry, dict) else None
            if isinstance(fixture_entry_id, str) and fixture_entry_id.strip():
                existing_ids.add(fixture_entry_id.strip())

    created_entries = 0
    for entry in spec.get("history") or ():
        fixture_entry_id = str(entry.get("fixture_entry_id") or "").strip()
        if not fixture_entry_id or fixture_entry_id in existing_ids:
            continue
        payload = dict(entry)
        chat_history_service.add_message_to_history(
            user_id=user_concept_id,
            session_id=session_id,
            message=payload,
            namespace=namespace,
            organisation_concept_id=organisation_concept_id,
            role_in_org=role_in_org,
            skip_rag_indexing=True,
        )
        created_entries += 1

    return {
        "session_id": session_id,
        "session_name": session_name,
        "created_entries": created_entries,
    }


def _build_counterpart_specs() -> Iterable[dict[str, str]]:
    return (
        {
            "concept_id": "#V#browser_test_workflow_reviewer",
            "name": "Workflow Reviewer",
            "email": "workflow-reviewer+browser-fixture@strongailab.invalid",
            "role": "member",
        },
        {
            "concept_id": "#V#browser_test_paper_scout",
            "name": "Paper Scout",
            "email": "paper-scout+browser-fixture@strongailab.invalid",
            "role": "member",
        },
    )


def login_browser_test_user(
    *,
    window_session_id: Optional[str] = None,
) -> dict[str, Any]:
    config = get_browser_test_auth_config()
    organisation_doc = _ensure_org_exists(config.organisation_concept_id)
    pseudouser_doc = _ensure_person_concept(
        concept_id=config.pseudouser_concept_id,
        name=config.pseudouser_name,
        email=config.pseudouser_email,
    )
    pseudouser_concept_id = str(
        pseudouser_doc.get("concept_id") or config.pseudouser_concept_id
    ).strip()
    pseudouser_slug = _slugify(
        pseudouser_concept_id[3:]
        if pseudouser_concept_id.startswith("#V#")
        else pseudouser_concept_id
    )
    org_slug = _slugify(config.organisation_concept_id[3:])
    namespace = derive_namespace(pseudouser_slug, org_slug)
    role_in_org = "member"

    _ensure_org_membership(
        user_concept_id=pseudouser_concept_id,
        organisation_concept_id=config.organisation_concept_id,
        role=role_in_org,
    )

    counterpart_docs: list[dict[str, Any]] = []
    for spec in _build_counterpart_specs():
        user_doc = _ensure_person_concept(
            concept_id=spec["concept_id"],
            name=spec["name"],
            email=spec["email"],
        )
        concept_id = str(user_doc.get("concept_id") or spec["concept_id"]).strip()
        _ensure_org_membership(
            user_concept_id=concept_id,
            organisation_concept_id=config.organisation_concept_id,
            role=spec["role"],
        )
        counterpart_docs.append(
            {
                "fixture_concept_id": spec["concept_id"],
                "concept_id": concept_id,
                "name": _get_concept_name(user_doc, spec["name"]),
                "email": spec["email"],
                "role": spec["role"],
            }
        )

    session["user_email"] = config.pseudouser_email
    session["google_user_info"] = {
        "name": config.pseudouser_name,
        "email": config.pseudouser_email,
    }
    session["user_concept_id"] = pseudouser_concept_id
    session["user_id"] = pseudouser_slug
    session["organisation_concept_id"] = config.organisation_concept_id
    session["role_in_org"] = role_in_org
    session["namespace"] = namespace
    session["auth_provider"] = "browser_test_fixture"
    session["browser_test_fixture_id"] = _BROWSER_TEST_FIXTURE_ID
    session.modified = True

    if isinstance(window_session_id, str) and window_session_id.strip():
        set_window_organisation(
            window_session_id=window_session_id.strip(),
            organisation_concept_id=config.organisation_concept_id,
            role_in_org=role_in_org,
            namespace=namespace,
            user_id=pseudouser_concept_id,
        )

    counterpart_by_fixture_concept_id = {
        item["fixture_concept_id"]: item for item in counterpart_docs
    }
    reviewer = counterpart_by_fixture_concept_id["#V#browser_test_workflow_reviewer"]
    scout = counterpart_by_fixture_concept_id["#V#browser_test_paper_scout"]

    message_results = [
        _ensure_fixture_message(
            spec=spec,
        )
        for spec in _message_fixture_specs(
            pseudouser_concept_id=pseudouser_concept_id,
            reviewer_concept_id=reviewer["concept_id"],
            scout_concept_id=scout["concept_id"],
            organisation_concept_id=config.organisation_concept_id,
        )
    ]
    chat_results = [
        _ensure_fixture_chat_session(
            user_concept_id=pseudouser_concept_id,
            namespace=namespace,
            organisation_concept_id=config.organisation_concept_id,
            role_in_org=role_in_org,
            spec=spec,
        )
        for spec in _chat_fixture_specs()
    ]

    active_chat_session_id = chat_results[0]["session_id"] if chat_results else None
    if (
        active_chat_session_id
        and isinstance(window_session_id, str)
        and window_session_id.strip()
    ):
        set_window_chat_session(
            window_session_id=window_session_id.strip(),
            chat_session_id=active_chat_session_id,
            user_id=pseudouser_concept_id,
        )

    created_message_count = sum(1 for item in message_results if item.get("created"))
    reused_message_count = len(message_results) - created_message_count
    created_history_entries = sum(int(item.get("created_entries") or 0) for item in chat_results)

    return {
        "user": {
            "concept_id": pseudouser_concept_id,
            "name": config.pseudouser_name,
            "email": config.pseudouser_email,
        },
        "organisation": {
            "concept_id": config.organisation_concept_id,
            "name": _get_concept_name(
                organisation_doc,
                config.organisation_concept_id,
            ),
            "role": role_in_org,
        },
        "namespace": namespace,
        "window_session_id": window_session_id,
        "active_chat_session_id": active_chat_session_id,
        "fixture": {
            "fixture_id": _BROWSER_TEST_FIXTURE_ID,
            "counterparts": counterpart_docs,
            "messages": {
                "total": len(message_results),
                "created": created_message_count,
                "reused": reused_message_count,
                "concept_ids": [item.get("concept_id") for item in message_results],
            },
            "chat_sessions": {
                "total": len(chat_results),
                "created_entries": created_history_entries,
                "sessions": chat_results,
            },
        },
    }


__all__ = [
    "browser_test_auth_allowed_for_request",
    "describe_browser_test_mode",
    "get_browser_test_auth_config",
    "login_browser_test_user",
]
