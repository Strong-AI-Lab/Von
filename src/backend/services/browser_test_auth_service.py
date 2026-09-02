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
from typing import Any, Iterable, Mapping, Optional

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
from ..services.paper_recommendation_constants import (
    PAPER_RECOMMENDATION_DELIVERED_VIA_MESSAGE_PREDICATE_ID,
    PAPER_RECOMMENDATION_POLICY_VERSION,
    SCHOLARLY_ARTICLE_TYPE_ID,
)
from ..services.paper_recommendation_vontology_service import (
    upsert_paper_recommendation_assertion,
)
from ..services.relationship_write_service import add_relationship
from ..services.text_value_service import upsert_text_for_concept
from ..services.window_session_context_service import (
    set_window_chat_session,
    set_window_organisation,
)
from .coding_agent_identity_bootstrap_service import CODING_AGENT_TYPE_ID, VON_SYSTEM_ID

_BROWSER_TEST_FIXTURE_ID = "browser_user_view.v1"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_LOOPBACK_REMOTES = {"127.0.0.1", "::1", "localhost", ""}
_BROWSER_TEST_ENV_KEYS = (
    "VON_BROWSER_TEST_AUTH_ENABLED",
    "VON_BROWSER_TEST_REFRESH_FIXTURE_ON_LOGIN",
    "VON_BROWSER_TEST_PSEUDOUSER_NAME",
    "VON_BROWSER_TEST_PSEUDOUSER_EMAIL",
    "VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID",
    "VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID",
)
_BROWSER_TEST_FIXTURE_STABLE_SEEDED_AT = "2026-03-14T00:00:00+00:00"
_BROWSER_TEST_RECOMMENDATION_PAPER_ID = (
    "#V#browser_test_paper_workflow_grounded_neuro_symbolic_planning"
)
_BROWSER_TEST_RECOMMENDATION_PAPER_TITLE = (
    "Workflow-Grounded Neuro-Symbolic Planning for Research Agents"
)
_BROWSER_TEST_RECOMMENDATION_PUBLICATION_DATE = "2026-03-14"
_BROWSER_TEST_RECOMMENDATION_SUMMARY = (
    "A browser-fixture paper about neuro-symbolic planning, workflow-grounded "
    "evaluation, and durable represented authority surfaces."
)
_BROWSER_TEST_RECOMMENDATION_RATIONALE = (
    "Matches stated interests: neuro-symbolic planning, workflow-grounded "
    "evaluation, and represented-authority design."
)
_BROWSER_TEST_PUBLICATION_DATE_PREDICATE_ID = "#V#has_publication_date"


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


def _trimmed_env(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _refresh_fixture_on_login_default() -> bool:
    return _truthy_env("VON_BROWSER_TEST_REFRESH_FIXTURE_ON_LOGIN", default=False)


def _browser_test_identity_source() -> str:
    explicit_identity = any(
        _trimmed_env(name)
        for name in (
            "VON_BROWSER_TEST_PSEUDOUSER_NAME",
            "VON_BROWSER_TEST_PSEUDOUSER_EMAIL",
            "VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID",
            "VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID",
        )
    )
    return "env_configured" if explicit_identity else "default"


def _browser_test_identity_label(config: "BrowserTestAuthConfig") -> str:
    return f"{config.pseudouser_name} <{config.pseudouser_email}>"


def _browser_test_setup_hint(
    config: "BrowserTestAuthConfig",
    *,
    available: bool,
    reason: str | None,
) -> str:
    if available:
        return (
            "Browser-test auth is available on localhost. Use Browser Test Login "
            f"to sign in as {_browser_test_identity_label(config)}."
        )
    if not config.enabled:
        return (
            "Set VON_BROWSER_TEST_AUTH_ENABLED=1 in your local environment or .env, "
            "then restart Von to enable Browser Test Login on localhost."
        )
    if reason:
        return (
            f"Browser-test auth is configured, but unavailable here: {reason}. "
            "Use a localhost or 127.0.0.1 browser session from the local machine."
        )
    return (
        "Browser-test auth is configured but unavailable. Use a localhost "
        "browser session from the local machine."
    )


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
    forwarded_host = (
        str(request.headers.get("X-Forwarded-Host") or "")
        .split(",", 1)[0]
        .strip()
        .split(":", 1)[0]
        .strip("[]")
        .lower()
    )
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
    identity_source = _browser_test_identity_source()
    status = (
        "available"
        if available
        else ("disabled" if not config.enabled else "configured_unavailable")
    )
    status_label = (
        "Available"
        if available
        else ("Disabled" if not config.enabled else "Configured but unavailable")
    )
    payload: dict[str, Any] = {
        "configured": config.enabled,
        "available": available,
        "reason": None if available else reason,
        "status": status,
        "status_label": status_label,
        "identity_source": identity_source,
        "identity_label": _browser_test_identity_label(config),
        "uses_default_identity": identity_source == "default",
        "localhost_only": True,
        "requires_restart": True,
        "setup_hint": _browser_test_setup_hint(
            config,
            available=available,
            reason=reason,
        ),
        "env_keys": list(_BROWSER_TEST_ENV_KEYS),
        "enabled_env_present": bool(_trimmed_env("VON_BROWSER_TEST_AUTH_ENABLED")),
    }
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
        # Browser-test authentication is an independently gated localhost-only
        # fixture. Its explicit configured concept ID, not any email relation,
        # selects the pseudouser. The email written below is contact data only.
        try:
            concept_doc = get_concept_by_concept_id(concept_id)
        except ConceptNotFoundError:
            concept_doc = create_concept(
                name=name,
                concept_id=concept_id,
                parent_concept_ids=["#V#person"],
            )
        if not isinstance(concept_doc, dict):
            raise RuntimeError(f"Browser-test concept lookup failed for {concept_id}")

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


def _browser_test_fixture_provenance(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "browser_test_fixture",
        "fixture_id": _BROWSER_TEST_FIXTURE_ID,
    }
    for key, value in extra.items():
        if value is None:
            continue
        payload[str(key)] = value
    return payload


def _browser_test_message_participant_signature(
    *,
    sender_id: str,
    recipient_ids: Iterable[str],
) -> str:
    sender = str(sender_id or "").strip()
    recipients = sorted(
        {
            str(recipient_id).strip()
            for recipient_id in (recipient_ids or [])
            if str(recipient_id or "").strip()
        }
    )
    return f"{sender}|{'|'.join(recipients)}"


def _build_fixture_message_metadata(*, spec: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(spec.get("metadata") or {})
    metadata.update(
        {
            "browser_test_fixture_id": _BROWSER_TEST_FIXTURE_ID,
            "browser_test_message_key": str(spec["key"]),
            "browser_test_seeded_at": _BROWSER_TEST_FIXTURE_STABLE_SEEDED_AT,
        }
    )
    target_user_concept_id = str(spec.get("target_user_concept_id") or "").strip()
    if target_user_concept_id:
        metadata["browser_test_target_user_concept_id"] = target_user_concept_id
    metadata["browser_test_participant_signature"] = (
        _browser_test_message_participant_signature(
            sender_id=str(spec.get("sender_id") or ""),
            recipient_ids=spec.get("recipient_ids") or [],
        )
    )
    return metadata


def _link_fixture_message_metadata(
    *,
    message_concept_id: str,
    metadata: Mapping[str, Any],
) -> None:
    if not isinstance(metadata, Mapping):
        return
    if (
        str(metadata.get("delivery_channel") or "").strip()
        != "paper_recommendation_message"
    ):
        return
    assertion_ids = metadata.get("recommendation_assertion_ids")
    if not isinstance(assertion_ids, list):
        return
    for assertion_id in assertion_ids:
        clean_assertion_id = str(assertion_id or "").strip()
        if not clean_assertion_id:
            continue
        add_relationship(
            source_id=clean_assertion_id,
            predicate=PAPER_RECOMMENDATION_DELIVERED_VIA_MESSAGE_PREDICATE_ID,
            target=message_concept_id,
        )


def _ensure_fixture_recommendation_paper(*, concept_id: str) -> dict[str, Any]:
    with bypass_access_control():
        try:
            concept_doc = get_concept_by_concept_id(concept_id)
        except ConceptNotFoundError:
            concept_doc = create_concept(
                name=_BROWSER_TEST_RECOMMENDATION_PAPER_TITLE,
                concept_id=concept_id,
                description=_BROWSER_TEST_RECOMMENDATION_SUMMARY,
                parent_concept_ids=[SCHOLARLY_ARTICLE_TYPE_ID],
                create_as_instance=True,
            )

        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text=_BROWSER_TEST_RECOMMENDATION_SUMMARY,
            provenance=_browser_test_fixture_provenance(
                fixture_surface="paper_recommendation_message",
            ),
        )
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=_BROWSER_TEST_PUBLICATION_DATE_PREDICATE_ID,
            text=_BROWSER_TEST_RECOMMENDATION_PUBLICATION_DATE,
            provenance=_browser_test_fixture_provenance(
                fixture_surface="paper_recommendation_message",
            ),
        )
        return (
            concept_doc if isinstance(concept_doc, dict) else {"concept_id": concept_id}
        )


def _paper_recommendation_fixture_spec(
    *,
    pseudouser_concept_id: str,
    organisation_concept_id: str,
) -> dict[str, Any]:
    paper_concept_id = _BROWSER_TEST_RECOMMENDATION_PAPER_ID
    _ensure_fixture_recommendation_paper(concept_id=paper_concept_id)
    evaluation_payload = {
        "schema_version": "paper_recommendation_evaluation.v1",
        "status": "ranked",
        "active": True,
        "score": 0.88,
        "recommendation_tier": "recommended",
        "policy_version": PAPER_RECOMMENDATION_POLICY_VERSION,
        "decision_mode": "browser_test_fixture",
        "trigger_source": "browser_test_fixture_refresh",
        "updated_at": _BROWSER_TEST_FIXTURE_STABLE_SEEDED_AT,
        "rationale_summary": _BROWSER_TEST_RECOMMENDATION_RATIONALE,
        "rationale": [_BROWSER_TEST_RECOMMENDATION_RATIONALE],
        "evidence": [
            {
                "evidence_type": "interest_term_match",
                "profile_value": "workflow-grounded evaluation",
                "matched_paper_text": "workflow-grounded evaluation of research agents",
                "paper_reference_id": "browser-fixture-paper-summary",
            }
        ],
        "paper_representation": {
            "publication_date": _BROWSER_TEST_RECOMMENDATION_PUBLICATION_DATE,
            "topic_labels": [
                "neuro-symbolic planning",
                "workflow-grounded evaluation",
                "represented authority",
            ],
            "author_names": [
                "Browser Fixture Research Collective",
            ],
        },
        "provenance": _browser_test_fixture_provenance(
            fixture_surface="message_panel.paper_recommendation_review",
        ),
    }
    assertion = upsert_paper_recommendation_assertion(
        subject_concept_id=pseudouser_concept_id,
        paper_concept_id=paper_concept_id,
        evaluation_payload=evaluation_payload,
        rationale_summary=_BROWSER_TEST_RECOMMENDATION_RATIONALE,
        provenance=_browser_test_fixture_provenance(
            fixture_surface="message_panel.paper_recommendation_review",
        ),
        context={
            "browser_test_fixture_id": _BROWSER_TEST_FIXTURE_ID,
            "subject_concept_id": pseudouser_concept_id,
            "paper_concept_id": paper_concept_id,
        },
    )
    assertion_id = str(assertion.get("assertion_concept_id") or "").strip()
    if not assertion_id:
        raise RuntimeError(
            "Browser-test recommendation fixture failed to create an assertion"
        )

    return {
        "key": "paper-recommendation",
        "sender_id": VON_SYSTEM_ID,
        "recipient_ids": [pseudouser_concept_id],
        "subject": "New paper recommendation from Von",
        "content": (
            "I found a paper that looks relevant to your research profile. "
            "Open Review recommendations to inspect the rationale, provenance, "
            "and record feedback directly on the delivered message."
        ),
        "metadata": {
            "attribution": "Sent by Von",
            "delivery_channel": "paper_recommendation_message",
            "intent": "paper_recommendation",
            "trigger_source": "browser_test_fixture_refresh",
            "recommendation_subject_concept_id": pseudouser_concept_id,
            "recommendation_assertion_ids": [assertion_id],
            "recommendation_paper_concept_ids": [paper_concept_id],
            "recommendation_count": 1,
        },
        "mark_unread_for": pseudouser_concept_id,
        "org_id": organisation_concept_id,
        "target_user_concept_id": pseudouser_concept_id,
    }


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
            "target_user_concept_id": pseudouser_concept_id,
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
            "target_user_concept_id": pseudouser_concept_id,
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
            "target_user_concept_id": pseudouser_concept_id,
        },
        _paper_recommendation_fixture_spec(
            pseudouser_concept_id=pseudouser_concept_id,
            organisation_concept_id=organisation_concept_id,
        ),
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
            "conversation_situation": {
                "text": (
                    "The user and Von are validating the authenticated browser "
                    "experience. Their shared goal is to keep saved conversations, "
                    "unread messages, organisation context, and the conversation "
                    "situation inspectable at desktop and mobile widths. Refreshing "
                    "this browser-test fixture must not duplicate its durable records."
                ),
                "source_request_id": "browser_user_view.v1:situation",
            },
            "conversation_observations": (
                {
                    "observation_id": "browser_user_view.v1:fixture-ready",
                    "kind": "fixture_state",
                    "observed_at_utc": _BROWSER_TEST_FIXTURE_STABLE_SEEDED_AT,
                    "capability_name": "authenticated_browser_user_view",
                    "terminal_status": "ready",
                    "outcome_finality": "fixture",
                    "changed": False,
                },
            ),
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
    metadata = _build_fixture_message_metadata(spec=spec)
    query = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        "concept_data.metadata.browser_test_fixture_id": metadata[
            "browser_test_fixture_id"
        ],
        "concept_data.metadata.browser_test_message_key": key,
    }
    target_user_concept_id = str(
        metadata.get("browser_test_target_user_concept_id") or ""
    ).strip()
    if target_user_concept_id:
        query["concept_data.metadata.browser_test_target_user_concept_id"] = (
            target_user_concept_id
        )
    participant_signature = str(
        metadata.get("browser_test_participant_signature") or ""
    ).strip()
    if participant_signature:
        query["concept_data.metadata.browser_test_participant_signature"] = (
            participant_signature
        )
    with bypass_access_control():
        existing = coll.find_one(query, {"concept_id": 1, "concept_data.read_by": 1})
    if isinstance(existing, dict) and existing.get("concept_id"):
        reset_payload: dict[str, Any] = {
            "updated_at": datetime.now(timezone.utc),
            "concept_data.subject": spec.get("subject"),
            "concept_data.content_fallback": spec.get("content"),
            "concept_data.message_status": MESSAGE_STATUS_SENT,
            "concept_data.deleted": False,
            "concept_data.metadata": metadata,
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
            _link_fixture_message_metadata(
                message_concept_id=existing["concept_id"],
                metadata=metadata,
            )
        return {"concept_id": existing["concept_id"], "created": False}

    created = create_message(
        sender_id=spec["sender_id"],
        recipient_ids=list(spec["recipient_ids"]),
        content=str(spec["content"]),
        subject=spec.get("subject"),
        org_id=spec.get("org_id"),
        metadata=metadata,
    )
    message_concept_id = str(created.get("concept_id") or "").strip()
    if message_concept_id:
        _link_fixture_message_metadata(
            message_concept_id=message_concept_id,
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
        origin_kind=chat_history_service.CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE,
        created_by_actor_concept_id=VON_SYSTEM_ID,
        created_by_actor_type=CODING_AGENT_TYPE_ID,
        is_agent_created=True,
        test_artifact_kind="browser_test_fixture_chat_session",
    )

    coll = chat_history_service.get_chat_history_collection_service()
    if coll is None:
        raise RuntimeError(
            "Chat history collection unavailable for browser-test fixture"
        )

    with bypass_access_control():
        doc = coll.find_one(
            {"user_id": user_concept_id, "session_id": session_id},
            {
                "history.fixture_entry_id": 1,
                "conversation_situation": 1,
                "conversation_observations.observation_id": 1,
            },
        )

    existing_ids: set[str] = set()
    if isinstance(doc, dict):
        for entry in doc.get("history") or []:
            fixture_entry_id = (
                entry.get("fixture_entry_id") if isinstance(entry, dict) else None
            )
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

    situation_updated = False
    situation_spec = spec.get("conversation_situation")
    if isinstance(situation_spec, Mapping):
        situation_text = str(situation_spec.get("text") or "").strip()
        existing_situation = (
            doc.get("conversation_situation") if isinstance(doc, Mapping) else None
        )
        existing_text = (
            str(existing_situation.get("text") or "").strip()
            if isinstance(existing_situation, Mapping)
            else ""
        )
        existing_revision = (
            existing_situation.get("revision")
            if isinstance(existing_situation, Mapping)
            and isinstance(existing_situation.get("revision"), int)
            and not isinstance(existing_situation.get("revision"), bool)
            else 0
        )
        if situation_text and situation_text != existing_text:
            result = chat_history_service.set_chat_history_conversation_situation(
                user_id=user_concept_id,
                session_id=session_id,
                text=situation_text,
                expected_revision=max(0, existing_revision),
                source="browser_test_fixture",
                updated_by=VON_SYSTEM_ID,
                namespace=namespace,
                include_legacy=False,
                source_request_id=str(
                    situation_spec.get("source_request_id")
                    or f"{_BROWSER_TEST_FIXTURE_ID}:situation"
                ),
            )
            situation_updated = bool(result.get("updated"))

    observations_added = 0
    for observation in spec.get("conversation_observations") or ():
        if not isinstance(observation, dict):
            continue
        result = chat_history_service.append_chat_history_conversation_observation(
            user_id=user_concept_id,
            session_id=session_id,
            observation=observation,
            namespace=namespace,
            include_legacy=False,
        )
        if result.get("updated"):
            observations_added += 1

    return {
        "session_id": session_id,
        "session_name": session_name,
        "created_entries": created_entries,
        "situation_updated": situation_updated,
        "observations_added": observations_added,
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


def _skipped_fixture_refresh_payload(
    *,
    counterpart_docs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "fixture_id": _BROWSER_TEST_FIXTURE_ID,
        "status": "not_refreshed",
        "refresh_requested": False,
        "refresh_on_login_default": _refresh_fixture_on_login_default(),
        "reason": "Browser-test login established an authenticated session without refreshing fixture data.",
        "counterparts": counterpart_docs,
        "messages": {
            "total": 0,
            "created": 0,
            "reused": 0,
            "concept_ids": [],
        },
        "chat_sessions": {
            "total": 0,
            "created_entries": 0,
            "sessions": [],
        },
    }


def _ensure_browser_test_fixture_state(
    *,
    pseudouser_concept_id: str,
    counterpart_docs: list[dict[str, Any]],
    namespace: str,
    organisation_concept_id: str,
    role_in_org: str,
) -> tuple[dict[str, Any], Optional[str]]:
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
            organisation_concept_id=organisation_concept_id,
        )
    ]
    chat_results = [
        _ensure_fixture_chat_session(
            user_concept_id=pseudouser_concept_id,
            namespace=namespace,
            organisation_concept_id=organisation_concept_id,
            role_in_org=role_in_org,
            spec=spec,
        )
        for spec in _chat_fixture_specs()
    ]
    active_chat_session_id = chat_results[0]["session_id"] if chat_results else None
    created_message_count = sum(1 for item in message_results if item.get("created"))
    reused_message_count = len(message_results) - created_message_count
    created_history_entries = sum(
        int(item.get("created_entries") or 0) for item in chat_results
    )

    return (
        {
            "fixture_id": _BROWSER_TEST_FIXTURE_ID,
            "status": "refreshed",
            "refresh_requested": True,
            "refresh_on_login_default": _refresh_fixture_on_login_default(),
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
        active_chat_session_id,
    )


def login_browser_test_user(
    *,
    window_session_id: Optional[str] = None,
    refresh_fixture: Optional[bool] = None,
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

    should_refresh_fixture = (
        _refresh_fixture_on_login_default()
        if refresh_fixture is None
        else bool(refresh_fixture)
    )
    if should_refresh_fixture:
        fixture_payload, active_chat_session_id = _ensure_browser_test_fixture_state(
            pseudouser_concept_id=pseudouser_concept_id,
            namespace=namespace,
            organisation_concept_id=config.organisation_concept_id,
            role_in_org=role_in_org,
            counterpart_docs=counterpart_docs,
        )
    else:
        fixture_payload = _skipped_fixture_refresh_payload(
            counterpart_docs=counterpart_docs,
        )
        active_chat_session_id = None

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
        "fixture": fixture_payload,
    }


__all__ = [
    "browser_test_auth_allowed_for_request",
    "describe_browser_test_mode",
    "get_browser_test_auth_config",
    "login_browser_test_user",
]
