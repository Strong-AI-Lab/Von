"""Server-bound signed references for conversation-scoped evidence tools."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Mapping

CONVERSATION_SCOPE_BINDING_SCHEMA_VERSION = "conversation_scope_binding.v1"
HISTORY_LOCATION_BINDING_SCHEMA_VERSION = "history_location_binding.v1"

_CONVERSATION_SCOPE_KIND = "conversation_scope"
_HISTORY_LOCATION_KIND = "history_location"
_DEFAULT_BINDING_SECRET = "von-dev-secret-key-change-in-production"


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


def build_conversation_scope_binding(
    *,
    chat_session_id: str,
    history_owner_user_id: str | None = None,
    read_namespace: str | None = None,
    organisation_concept_id: str | None = None,
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
        },
    )


def verify_conversation_scope_binding(binding: Any) -> dict[str, Any]:
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

    identifier_binding = dict(verified["identifier_binding"])
    identifier_binding["chat_session_id_source"] = "conversation_ref"
    return {
        "success": True,
        "chat_session_id": chat_session_id,
        "history_owner_user_id": _safe_str(payload.get("history_owner_user_id")),
        "read_namespace": _safe_str(payload.get("read_namespace")),
        "organisation_concept_id": _safe_str(payload.get("organisation_concept_id")),
        "identifier_binding": identifier_binding,
    }


def build_history_location_binding(
    *,
    chat_session_id: str,
    history_index: int,
    history_owner_user_id: str | None = None,
    read_namespace: str | None = None,
    organisation_concept_id: str | None = None,
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
        },
    )


def verify_history_location_binding(binding: Any) -> dict[str, Any]:
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

    identifier_binding = dict(verified["identifier_binding"])
    identifier_binding["chat_session_id_source"] = "history_location_ref"
    identifier_binding["history_index_source"] = "history_location_ref"
    return {
        "success": True,
        "chat_session_id": chat_session_id,
        "history_index": history_index,
        "history_owner_user_id": _safe_str(payload.get("history_owner_user_id")),
        "read_namespace": _safe_str(payload.get("read_namespace")),
        "organisation_concept_id": _safe_str(payload.get("organisation_concept_id")),
        "identifier_binding": identifier_binding,
    }
