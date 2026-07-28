"""Server-bound signed references for conversation-scoped evidence tools."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

CONVERSATION_SCOPE_BINDING_SCHEMA_VERSION = "conversation_scope_binding.v1"
HISTORY_LOCATION_BINDING_SCHEMA_VERSION = "history_location_binding.v1"
TURN_TELEMETRY_BINDING_SCHEMA_VERSION = "turn_telemetry_binding.v1"
TELEMETRY_READ_DELEGATION_AUDIENCE = "vontology_stdio_mcp"

_CONVERSATION_SCOPE_KIND = "conversation_scope"
_HISTORY_LOCATION_KIND = "history_location"
_TURN_TELEMETRY_KIND = "turn_telemetry"
_DEFAULT_BINDING_SECRET = "von-dev-secret-key-change-in-production"
_DEFAULT_READ_DELEGATION_TTL_SECONDS = 15 * 60
_MAX_READ_DELEGATION_TTL_SECONDS = 60 * 60


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _safe_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(
        value, (bytes, bytearray, str)
    ):
        candidates = value
    else:
        return []
    return sorted(
        {
            cleaned
            for item in candidates
            if (cleaned := _safe_str(item)) is not None
        }
    )


def _normalise_utc_datetime(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return _normalise_utc_datetime(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    cleaned = _safe_str(value)
    if not cleaned:
        return None
    candidate = f"{cleaned[:-1]}+00:00" if cleaned.endswith("Z") else cleaned
    try:
        parsed = datetime.fromisoformat(candidate)
    except Exception:
        return None
    return _normalise_utc_datetime(parsed)


def _read_delegation_fields(
    *,
    delegated_actor_user_id: str | None,
    permitted_tools: Sequence[str],
    audience: str,
    ttl_seconds: int | None,
    issued_at: datetime | None,
    nonce: str | None,
) -> dict[str, Any]:
    actor = _safe_str(delegated_actor_user_id)
    tools = _safe_string_list(permitted_tools)
    audience_value = _safe_str(audience)
    if not actor:
        raise ValueError("delegated_actor_user_id is required")
    if not tools:
        raise ValueError("at least one permitted telemetry read tool is required")
    if not audience_value:
        raise ValueError("audience is required")

    ttl = _safe_int(ttl_seconds)
    if ttl is None:
        ttl = _DEFAULT_READ_DELEGATION_TTL_SECONDS
    ttl = min(_MAX_READ_DELEGATION_TTL_SECONDS, max(1, ttl))
    issued = _normalise_utc_datetime(issued_at)
    expires = issued + timedelta(seconds=ttl)
    return {
        "delegated_actor_user_id": actor,
        "audience": audience_value,
        "permitted_tools": tools,
        "issued_at_utc": _utc_iso(issued),
        "expires_at_utc": _utc_iso(expires),
        "nonce": _safe_str(nonce) or secrets.token_urlsafe(12),
    }


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _binding_secret_bytes() -> bytes:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            secret = getattr(current_app, "secret_key", None)
            if isinstance(secret, bytes) and secret:
                return secret
            if isinstance(secret, str) and secret.strip():
                return secret.strip().encode("utf-8")
    except Exception:
        pass

    env_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    return _DEFAULT_BINDING_SECRET.encode("utf-8")


def _binding_fingerprint(unsigned_payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(unsigned_payload).encode("utf-8")).hexdigest()[
        :16
    ]


def _signature_for_payload(unsigned_payload: Mapping[str, Any]) -> str:
    return hmac.new(
        _binding_secret_bytes(),
        _canonical_json(unsigned_payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_signed_binding(
    *,
    schema_version: str,
    binding_kind: str,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    unsigned_payload = {
        "schema_version": schema_version,
        "binding_kind": binding_kind,
        **{key: value for key, value in fields.items() if value is not None},
    }
    return {
        **unsigned_payload,
        "binding_id": _binding_fingerprint(unsigned_payload),
        "signature": _signature_for_payload(unsigned_payload),
    }


def _verify_signed_binding(
    binding: Any,
    *,
    schema_version: str,
    binding_kind: str,
) -> dict[str, Any]:
    reference_kind = binding_kind
    if not isinstance(binding, Mapping):
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Bound reference must be an object",
            "identifier_binding": {
                "mode": "server_bound_reference",
                "reference_kind": reference_kind,
                "validation_status": "not_a_mapping",
            },
        }

    schema_value = _safe_str(binding.get("schema_version"))
    kind_value = _safe_str(binding.get("binding_kind"))
    signature = _safe_str(binding.get("signature"))
    binding_id = _safe_str(binding.get("binding_id"))

    identifier_binding = {
        "mode": "server_bound_reference",
        "reference_kind": kind_value or reference_kind,
        "binding_id": binding_id,
        "validation_status": "pending",
    }

    if schema_value != schema_version or kind_value != binding_kind:
        identifier_binding["validation_status"] = "schema_or_kind_mismatch"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Bound reference schema_version or binding_kind is invalid",
            "identifier_binding": identifier_binding,
        }

    if not signature:
        identifier_binding["validation_status"] = "missing_signature"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Bound reference signature is missing",
            "identifier_binding": identifier_binding,
        }

    unsigned_payload = {
        str(key): value
        for key, value in binding.items()
        if isinstance(key, str) and key not in {"signature", "binding_id"}
    }
    expected_binding_id = _binding_fingerprint(unsigned_payload)
    expected_signature = _signature_for_payload(unsigned_payload)
    if binding_id and binding_id != expected_binding_id:
        identifier_binding["validation_status"] = "binding_id_mismatch"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Bound reference fingerprint is invalid",
            "identifier_binding": identifier_binding,
        }
    if not hmac.compare_digest(signature, expected_signature):
        identifier_binding["validation_status"] = "signature_mismatch"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Bound reference signature is invalid",
            "identifier_binding": identifier_binding,
        }

    identifier_binding["validation_status"] = "verified"
    identifier_binding["signature_verified"] = True
    identifier_binding["binding_id"] = expected_binding_id
    return {
        "success": True,
        "payload": unsigned_payload,
        "identifier_binding": identifier_binding,
    }


def _verify_read_delegation_claims(
    *,
    payload: Mapping[str, Any],
    identifier_binding: Mapping[str, Any],
    require_read_delegation: bool,
    expected_audience: str | None,
    expected_tool: str | None,
    expected_actor_user_id: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    audience = _safe_str(payload.get("audience"))
    actor = _safe_str(payload.get("delegated_actor_user_id"))
    permitted_tools = _safe_string_list(payload.get("permitted_tools"))
    issued_at = _parse_utc(payload.get("issued_at_utc"))
    expires_at = _parse_utc(payload.get("expires_at_utc"))
    nonce = _safe_str(payload.get("nonce"))
    has_complete_delegation = bool(
        audience
        and actor
        and permitted_tools
        and issued_at
        and expires_at
        and nonce
    )

    enriched_binding = {
        **dict(identifier_binding),
        "delegation_audience": audience,
        "delegated_actor_user_id": actor,
        "permitted_tools": permitted_tools,
        "issued_at_utc": _utc_iso(issued_at) if issued_at else None,
        "expires_at_utc": _utc_iso(expires_at) if expires_at else None,
    }
    if not has_complete_delegation:
        if not require_read_delegation:
            return {
                "success": True,
                "read_delegation": None,
                "identifier_binding": enriched_binding,
            }
        enriched_binding["validation_status"] = "read_delegation_claims_missing"
        return {
            "success": False,
            "error_code": "READ_DELEGATION_REQUIRED",
            "error_message": (
                "The signed reference does not carry a complete external MCP "
                "read delegation"
            ),
            "identifier_binding": enriched_binding,
        }

    current_time = _normalise_utc_datetime(now)
    if expires_at <= issued_at:
        enriched_binding["validation_status"] = "invalid_delegation_window"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Read delegation expiry is not after its issue time",
            "identifier_binding": enriched_binding,
        }
    if current_time >= expires_at:
        enriched_binding["validation_status"] = "read_delegation_expired"
        return {
            "success": False,
            "error_code": "READ_DELEGATION_EXPIRED",
            "error_message": "Read delegation has expired",
            "identifier_binding": enriched_binding,
        }
    if expected_audience and audience != _safe_str(expected_audience):
        enriched_binding["validation_status"] = "read_delegation_audience_mismatch"
        return {
            "success": False,
            "error_code": "READ_DELEGATION_AUDIENCE_MISMATCH",
            "error_message": "Read delegation audience does not match this MCP surface",
            "identifier_binding": enriched_binding,
        }
    if expected_tool and _safe_str(expected_tool) not in permitted_tools:
        enriched_binding["validation_status"] = "read_delegation_tool_mismatch"
        return {
            "success": False,
            "error_code": "READ_DELEGATION_TOOL_MISMATCH",
            "error_message": "Read delegation does not permit this MCP tool",
            "identifier_binding": enriched_binding,
        }
    expected_actor = _safe_str(expected_actor_user_id)
    if expected_actor and actor != expected_actor:
        enriched_binding["validation_status"] = "read_delegation_actor_mismatch"
        return {
            "success": False,
            "error_code": "READ_DELEGATION_ACTOR_MISMATCH",
            "error_message": "Read delegation is bound to a different actor",
            "identifier_binding": enriched_binding,
        }

    enriched_binding["read_delegation_validation_status"] = "verified"
    return {
        "success": True,
        "read_delegation": {
            "binding_id": enriched_binding.get("binding_id"),
            "audience": audience,
            "delegated_actor_user_id": actor,
            "permitted_tools": permitted_tools,
            "issued_at_utc": _utc_iso(issued_at),
            "expires_at_utc": _utc_iso(expires_at),
        },
        "identifier_binding": enriched_binding,
    }


def build_conversation_scope_binding(
    *,
    chat_session_id: str,
    history_owner_user_id: str | None = None,
    read_namespace: str | None = None,
    organisation_concept_id: str | None = None,
    delegated_actor_user_id: str | None = None,
    delegated_actor_namespace: str | None = None,
    permitted_tools: Sequence[str] = (
        "conversation_telemetry_get_locator",
        "chat_history_get_segments",
    ),
    audience: str = TELEMETRY_READ_DELEGATION_AUDIENCE,
    ttl_seconds: int | None = None,
    issued_at: datetime | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    chat_session_id_value = _safe_str(chat_session_id)
    if not chat_session_id_value:
        raise ValueError("chat_session_id is required")

    return _build_signed_binding(
        schema_version=CONVERSATION_SCOPE_BINDING_SCHEMA_VERSION,
        binding_kind=_CONVERSATION_SCOPE_KIND,
        fields={
            "chat_session_id": chat_session_id_value,
            "history_owner_user_id": _safe_str(history_owner_user_id),
            "read_namespace": _safe_str(read_namespace),
            "organisation_concept_id": _safe_str(organisation_concept_id),
            "delegated_actor_namespace": (
                _safe_str(delegated_actor_namespace) or _safe_str(read_namespace)
            ),
            **_read_delegation_fields(
                delegated_actor_user_id=(
                    _safe_str(delegated_actor_user_id)
                    or _safe_str(history_owner_user_id)
                ),
                permitted_tools=permitted_tools,
                audience=audience,
                ttl_seconds=ttl_seconds,
                issued_at=issued_at,
                nonce=nonce,
            ),
        },
    )


def verify_conversation_scope_binding(
    binding: Any,
    *,
    require_read_delegation: bool = False,
    expected_audience: str | None = None,
    expected_tool: str | None = None,
    expected_actor_user_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    verified = _verify_signed_binding(
        binding,
        schema_version=CONVERSATION_SCOPE_BINDING_SCHEMA_VERSION,
        binding_kind=_CONVERSATION_SCOPE_KIND,
    )
    if not verified.get("success"):
        return verified

    payload = dict(verified["payload"])
    chat_session_id = _safe_str(payload.get("chat_session_id"))
    if not chat_session_id:
        identifier_binding = dict(verified["identifier_binding"])
        identifier_binding["validation_status"] = "missing_chat_session_id"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Bound conversation reference is missing chat_session_id",
            "identifier_binding": identifier_binding,
        }

    delegation = _verify_read_delegation_claims(
        payload=payload,
        identifier_binding=verified["identifier_binding"],
        require_read_delegation=require_read_delegation,
        expected_audience=expected_audience,
        expected_tool=expected_tool,
        expected_actor_user_id=expected_actor_user_id,
        now=now,
    )
    if not delegation.get("success"):
        return delegation

    identifier_binding = dict(delegation["identifier_binding"])
    identifier_binding["chat_session_id_source"] = "conversation_ref"
    return {
        "success": True,
        "chat_session_id": chat_session_id,
        "history_owner_user_id": _safe_str(payload.get("history_owner_user_id")),
        "read_namespace": _safe_str(payload.get("read_namespace")),
        "organisation_concept_id": _safe_str(payload.get("organisation_concept_id")),
        "delegated_actor_user_id": _safe_str(
            payload.get("delegated_actor_user_id")
        ),
        "delegated_actor_namespace": _safe_str(
            payload.get("delegated_actor_namespace")
        ),
        "read_delegation": delegation.get("read_delegation"),
        "identifier_binding": identifier_binding,
    }


def build_history_location_binding(
    *,
    chat_session_id: str,
    history_index: int,
    history_owner_user_id: str | None = None,
    read_namespace: str | None = None,
    organisation_concept_id: str | None = None,
    delegated_actor_user_id: str | None = None,
    delegated_actor_namespace: str | None = None,
    permitted_tools: Sequence[str] = ("chat_history_get_debug_entry",),
    audience: str = TELEMETRY_READ_DELEGATION_AUDIENCE,
    ttl_seconds: int | None = None,
    issued_at: datetime | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    chat_session_id_value = _safe_str(chat_session_id)
    history_index_value = _safe_int(history_index)
    if not chat_session_id_value:
        raise ValueError("chat_session_id is required")
    if history_index_value is None or history_index_value < 0:
        raise ValueError("history_index must be a non-negative integer")

    return _build_signed_binding(
        schema_version=HISTORY_LOCATION_BINDING_SCHEMA_VERSION,
        binding_kind=_HISTORY_LOCATION_KIND,
        fields={
            "chat_session_id": chat_session_id_value,
            "history_index": history_index_value,
            "history_owner_user_id": _safe_str(history_owner_user_id),
            "read_namespace": _safe_str(read_namespace),
            "organisation_concept_id": _safe_str(organisation_concept_id),
            "delegated_actor_namespace": (
                _safe_str(delegated_actor_namespace) or _safe_str(read_namespace)
            ),
            **_read_delegation_fields(
                delegated_actor_user_id=(
                    _safe_str(delegated_actor_user_id)
                    or _safe_str(history_owner_user_id)
                ),
                permitted_tools=permitted_tools,
                audience=audience,
                ttl_seconds=ttl_seconds,
                issued_at=issued_at,
                nonce=nonce,
            ),
        },
    )


def verify_history_location_binding(
    binding: Any,
    *,
    require_read_delegation: bool = False,
    expected_audience: str | None = None,
    expected_tool: str | None = None,
    expected_actor_user_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    verified = _verify_signed_binding(
        binding,
        schema_version=HISTORY_LOCATION_BINDING_SCHEMA_VERSION,
        binding_kind=_HISTORY_LOCATION_KIND,
    )
    if not verified.get("success"):
        return verified

    payload = dict(verified["payload"])
    chat_session_id = _safe_str(payload.get("chat_session_id"))
    history_index = _safe_int(payload.get("history_index"))
    if not chat_session_id or history_index is None or history_index < 0:
        identifier_binding = dict(verified["identifier_binding"])
        identifier_binding["validation_status"] = "missing_history_location_fields"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": (
                "Bound history location reference is missing chat_session_id or history_index"
            ),
            "identifier_binding": identifier_binding,
        }

    delegation = _verify_read_delegation_claims(
        payload=payload,
        identifier_binding=verified["identifier_binding"],
        require_read_delegation=require_read_delegation,
        expected_audience=expected_audience,
        expected_tool=expected_tool,
        expected_actor_user_id=expected_actor_user_id,
        now=now,
    )
    if not delegation.get("success"):
        return delegation

    identifier_binding = dict(delegation["identifier_binding"])
    identifier_binding["chat_session_id_source"] = "history_location_ref"
    identifier_binding["history_index_source"] = "history_location_ref"
    return {
        "success": True,
        "chat_session_id": chat_session_id,
        "history_index": history_index,
        "history_owner_user_id": _safe_str(payload.get("history_owner_user_id")),
        "read_namespace": _safe_str(payload.get("read_namespace")),
        "organisation_concept_id": _safe_str(payload.get("organisation_concept_id")),
        "delegated_actor_user_id": _safe_str(
            payload.get("delegated_actor_user_id")
        ),
        "delegated_actor_namespace": _safe_str(
            payload.get("delegated_actor_namespace")
        ),
        "read_delegation": delegation.get("read_delegation"),
        "identifier_binding": identifier_binding,
    }


def build_turn_telemetry_binding(
    *,
    request_id: str,
    delegated_actor_user_id: str,
    permitted_tool: str,
    chat_session_id: str | None = None,
    history_index: int | None = None,
    history_owner_user_id: str | None = None,
    read_namespace: str | None = None,
    delegated_actor_namespace: str | None = None,
    organisation_concept_id: str | None = None,
    audience: str = TELEMETRY_READ_DELEGATION_AUDIENCE,
    ttl_seconds: int | None = None,
    issued_at: datetime | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    request_id_value = _safe_str(request_id)
    tool_value = _safe_str(permitted_tool)
    if not request_id_value:
        raise ValueError("request_id is required")
    if not tool_value:
        raise ValueError("permitted_tool is required")
    history_index_value = (
        _safe_int(history_index) if history_index is not None else None
    )
    if history_index_value is not None and history_index_value < 0:
        raise ValueError("history_index must be a non-negative integer")

    return _build_signed_binding(
        schema_version=TURN_TELEMETRY_BINDING_SCHEMA_VERSION,
        binding_kind=_TURN_TELEMETRY_KIND,
        fields={
            "request_id": request_id_value,
            "chat_session_id": _safe_str(chat_session_id),
            "history_index": history_index_value,
            "history_owner_user_id": _safe_str(history_owner_user_id),
            "read_namespace": _safe_str(read_namespace),
            "delegated_actor_namespace": (
                _safe_str(delegated_actor_namespace) or _safe_str(read_namespace)
            ),
            "organisation_concept_id": _safe_str(organisation_concept_id),
            **_read_delegation_fields(
                delegated_actor_user_id=delegated_actor_user_id,
                permitted_tools=(tool_value,),
                audience=audience,
                ttl_seconds=ttl_seconds,
                issued_at=issued_at,
                nonce=nonce,
            ),
        },
    )


def verify_turn_telemetry_binding(
    binding: Any,
    *,
    expected_tool: str,
    expected_audience: str = TELEMETRY_READ_DELEGATION_AUDIENCE,
    expected_actor_user_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    verified = _verify_signed_binding(
        binding,
        schema_version=TURN_TELEMETRY_BINDING_SCHEMA_VERSION,
        binding_kind=_TURN_TELEMETRY_KIND,
    )
    if not verified.get("success"):
        return verified

    payload = dict(verified["payload"])
    request_id = _safe_str(payload.get("request_id"))
    if not request_id:
        identifier_binding = dict(verified["identifier_binding"])
        identifier_binding["validation_status"] = "missing_request_id"
        return {
            "success": False,
            "error_code": "INVALID_CONTEXT_BINDING",
            "error_message": "Bound turn telemetry reference is missing request_id",
            "identifier_binding": identifier_binding,
        }

    delegation = _verify_read_delegation_claims(
        payload=payload,
        identifier_binding=verified["identifier_binding"],
        require_read_delegation=True,
        expected_audience=expected_audience,
        expected_tool=expected_tool,
        expected_actor_user_id=expected_actor_user_id,
        now=now,
    )
    if not delegation.get("success"):
        return delegation

    history_index = (
        _safe_int(payload.get("history_index"))
        if payload.get("history_index") is not None
        else None
    )
    return {
        "success": True,
        "request_id": request_id,
        "chat_session_id": _safe_str(payload.get("chat_session_id")),
        "history_index": history_index,
        "history_owner_user_id": _safe_str(payload.get("history_owner_user_id")),
        "read_namespace": _safe_str(payload.get("read_namespace")),
        "organisation_concept_id": _safe_str(payload.get("organisation_concept_id")),
        "delegated_actor_user_id": _safe_str(
            payload.get("delegated_actor_user_id")
        ),
        "delegated_actor_namespace": _safe_str(
            payload.get("delegated_actor_namespace")
        ),
        "read_delegation": delegation.get("read_delegation"),
        "identifier_binding": delegation["identifier_binding"],
    }
