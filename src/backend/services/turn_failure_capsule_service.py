from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


TURN_FAILURE_CAPSULE_SCHEMA_VERSION = "turn_failure_capsule.v1"
TURN_FAILURE_CAPSULE_MAX_BYTES = 8 * 1024

_MAX_EFFECTS = 8
_VISIBLE_RESPONSE_LIMIT = 1400
_DRAFT_LIMIT = 800
_ERROR_PREVIEW_LIMIT = 360
_HANDLE_LIMIT = 180
_NAME_LIMIT = 140
_STATUS_LIMIT = 96
_ERROR_CODE_LIMIT = 140

_FAILURE_STATUSES = frozenset(
    {"failed", "partial", "indeterminate", "not_started", "blocked", "error"}
)
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
            flags=re.IGNORECASE | re.DOTALL,
        ),
        "[redacted-private-key]",
    ),
    (
        re.compile(
            r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic)\s+)([^\s,;]+)"
        ),
        r"\1[redacted]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"id[_-]?token|token|password|passwd|secret|client[_-]?secret|cookie|"
            r"session[_-]?token|signature|nonce)(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[redacted]"),
    (
        re.compile(
            r"\b(?:gh[opurs]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
            r"AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{20,}|"
            r"xox[baprs]-[0-9A-Za-z-]{10,})\b"
        ),
        "[redacted]",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\b"
        ),
        "[redacted]",
    ),
    (
        re.compile(
            r"(?i)\b((?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://)"
            r"[^@\s/]+@"
        ),
        r"\1[redacted]@",
    ),
    (
        re.compile(
            r"(?im)^[ \t]*File\s+[\"'][^\"']+[\"'],\s+line\s+\d+"
            r"(?:,\s+in\s+[^\n]+)?$(?:\n[ \t]+[^\n]+)?"
        ),
        "[redacted-trace-frame]",
    ),
    (
        re.compile(
            r"(?i)(?:[A-Z]:\\Users\\[^\s\"'<>]+|"
            r"/(?:Users|home)/[^\s\"'<>]+)"
        ),
        "[redacted-local-path]",
    ),
    (
        re.compile(r"(?i)<asyncio\.locks\.Lock object at 0x[0-9a-f]+>"),
        "asyncio lock",
    ),
    (re.compile(r"(?i)\b0x[0-9a-f]{6,}\b"), "[memory-address]"),
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compact_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return cleaned or None


def _redact_text(
    value: Any,
    *,
    sensitive_identity_values: Sequence[str],
    redaction_counter: list[int],
) -> str | None:
    text = _clean_string(value)
    if text is None:
        return None

    for identity in sorted(
        {
            item.strip()
            for item in sensitive_identity_values
            if isinstance(item, str) and item.strip()
        },
        key=lambda item: (-len(item), item),
    ):
        occurrences = text.count(identity)
        if occurrences:
            text = text.replace(identity, "[redacted-identity]")
            redaction_counter[0] += occurrences

    for pattern, replacement in _SECRET_PATTERNS:
        text, count = pattern.subn(replacement, text)
        redaction_counter[0] += count
    return text


def _bounded_scalar(
    value: Any,
    *,
    limit: int,
    sensitive_identity_values: Sequence[str],
    redaction_counter: list[int],
    truncation_counter: list[int] | None = None,
) -> str | None:
    text = _redact_text(
        value,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
    )
    if text is None:
        return None
    if "[redacted-identity]" in text:
        return None
    if len(text) <= limit:
        return text
    if truncation_counter is not None:
        truncation_counter[0] += 1
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _text_surface(
    value: Any,
    *,
    limit: int,
    sensitive_identity_values: Sequence[str],
    redaction_counter: list[int],
) -> dict[str, Any] | None:
    safe_text = _redact_text(
        value,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
    )
    if safe_text is None:
        return None
    truncated = len(safe_text) > limit
    preview = safe_text if not truncated else safe_text[:limit].rstrip()
    return {
        "text": preview,
        "char_count": len(safe_text),
        "sha256": hashlib.sha256(safe_text.encode("utf-8")).hexdigest(),
        "truncated": truncated,
    }


def _shrink_text_surface(surface: Any, limit: int) -> bool:
    if not isinstance(surface, dict):
        return False
    text = surface.get("text")
    if not isinstance(text, str) or len(text) <= limit:
        return False
    surface["text"] = text[:limit].rstrip()
    surface["truncated"] = True
    return True


def _canonical_readback_verdict(fact: Mapping[str, Any]) -> str | None:
    if fact.get("workflow_instance_readback_verified") is True:
        return "workflow_instance_verified"
    if fact.get("workflow_instance_operational_readback") is True:
        return "workflow_instance_unverified"
    if fact.get("canonical_readback_verified") is True:
        return "verified"
    if fact.get("canonical_readback_present") is True:
        return "present_unverified"
    if fact.get("canonical_readback_present") is False:
        return "not_present"
    return None


def _project_effect(
    fact: Mapping[str, Any],
    *,
    sensitive_identity_values: Sequence[str],
    redaction_counter: list[int],
    truncation_counter: list[int],
) -> dict[str, Any] | None:
    def scalar(value: Any, limit: int = _HANDLE_LIMIT) -> str | None:
        return _bounded_scalar(
            value,
            limit=limit,
            sensitive_identity_values=sensitive_identity_values,
            redaction_counter=redaction_counter,
            truncation_counter=truncation_counter,
        )

    effect_id = scalar(fact.get("effect_id"))
    name = scalar(fact.get("tool"), _NAME_LIMIT)
    status = scalar(fact.get("effect_status"), _STATUS_LIMIT)
    if not any((effect_id, name, status)):
        return None

    projected: dict[str, Any] = {}
    for key, value in (
        ("effect_id", effect_id),
        ("name", name),
        (
            "initial_status",
            scalar(fact.get("initial_effect_status"), _STATUS_LIMIT),
        ),
        ("status", status),
        (
            "current_outcome_status",
            scalar(fact.get("current_outcome_status"), _STATUS_LIMIT),
        ),
        (
            "reconciliation_status",
            scalar(fact.get("reconciliation_status"), _STATUS_LIMIT),
        ),
        ("canonical_readback_verdict", _canonical_readback_verdict(fact)),
        (
            "recovered_by_effect_id",
            scalar(fact.get("recovered_by_effect_id")),
        ),
        ("workflow_id", scalar(fact.get("workflow_id"))),
        ("instance_id", scalar(fact.get("instance_id"))),
        ("evidence_id", scalar(fact.get("evidence_id"))),
    ):
        if value is not None:
            projected[key] = value
    if isinstance(fact.get("changed"), bool):
        projected["changed"] = fact.get("changed")
    if isinstance(fact.get("outcome_resolved"), bool):
        projected["outcome_resolved"] = fact.get("outcome_resolved")

    error_code = scalar(fact.get("error_code"), _ERROR_CODE_LIMIT)
    error_preview = _text_surface(
        fact.get("error"),
        limit=_ERROR_PREVIEW_LIMIT,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
    )
    if error_code is not None or error_preview is not None:
        projected["error"] = {
            **({"code": error_code} if error_code is not None else {}),
            **({"preview": error_preview} if error_preview is not None else {}),
        }
    return projected


def _effect_priority(
    indexed_fact: tuple[int, Mapping[str, Any]],
) -> tuple[int, int]:
    index, fact = indexed_fact
    has_direct_error = bool(
        _clean_string(fact.get("error_code")) or _clean_string(fact.get("error"))
    )
    status = (_clean_string(fact.get("effect_status")) or "").lower()
    if has_direct_error:
        return (0, index)
    if status in _FAILURE_STATUSES:
        return (1, index)
    return (2, index)


def _build_turn_error(
    *,
    error_code: Any,
    error_text: Any,
    sensitive_identity_values: Sequence[str],
    redaction_counter: list[int],
    truncation_counter: list[int],
) -> dict[str, Any] | None:
    code = _bounded_scalar(
        error_code,
        limit=_ERROR_CODE_LIMIT,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
        truncation_counter=truncation_counter,
    )
    preview = _text_surface(
        error_text,
        limit=_ERROR_PREVIEW_LIMIT,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
    )
    if code is None and preview is None:
        return None
    return {
        **({"code": code} if code is not None else {}),
        **({"preview": preview} if preview is not None else {}),
    }


def _enforce_hard_bound(capsule: dict[str, Any]) -> None:
    """Deterministically stay within the portable 8 KiB contract.

    The first effect is the highest-priority direct failure. It is never removed.
    Terminal fields and any direct turn error likewise survive every reduction.
    """

    summary = capsule["effect_summary"]
    redaction = capsule["redaction"]

    def oversized() -> bool:
        return len(_compact_json_bytes(capsule)) > TURN_FAILURE_CAPSULE_MAX_BYTES

    while oversized() and len(capsule["effects"]) > 1:
        capsule["effects"].pop()
        summary["included_count"] -= 1
        summary["omitted_count"] += 1
        redaction["truncated"] = True

    for surface, limit in (
        (capsule.get("visible_response"), 700),
        (capsule.get("pre_presentation_draft"), 400),
    ):
        if oversized() and _shrink_text_surface(surface, limit):
            redaction["truncated"] = True
    for effect in capsule["effects"]:
        if not oversized():
            break
        error = effect.get("error")
        if isinstance(error, Mapping) and _shrink_text_surface(
            error.get("preview"), 180
        ):
            redaction["truncated"] = True

    if oversized() and capsule["effects"]:
        essential_keys = {
            "effect_id",
            "name",
            "initial_status",
            "status",
            "current_outcome_status",
            "outcome_resolved",
            "reconciliation_status",
            "canonical_readback_verdict",
            "error",
            "recovered_by_effect_id",
        }
        first_effect = capsule["effects"][0]
        capsule["effects"] = [
            {key: value for key, value in first_effect.items() if key in essential_keys}
        ]
        removed = summary["included_count"] - 1
        summary["included_count"] = 1
        summary["omitted_count"] += max(0, removed)
        redaction["truncated"] = True

    if oversized():
        _shrink_text_surface(capsule.get("visible_response"), 240)
        _shrink_text_surface(capsule.get("pre_presentation_draft"), 160)
        for effect in capsule["effects"]:
            error = effect.get("error")
            if isinstance(error, Mapping):
                _shrink_text_surface(error.get("preview"), 120)
        redaction["truncated"] = True

    if oversized():
        # All remaining strings are already bounded; this defensive final shape
        # retains the terminal and direct failure while dropping optional views.
        capsule.pop("visible_response", None)
        capsule.pop("pre_presentation_draft", None)
        capsule.pop("canonical_scope_modes", None)
        redaction["truncated"] = True

    if oversized():  # pragma: no cover - guard against future schema expansion
        raise ValueError("turn_failure_capsule_exceeds_hard_bound")


def build_turn_failure_capsule(
    *,
    request_id: Any,
    terminal_status: Any,
    response_authority: Any,
    visible_response: Any,
    outcome_report: Mapping[str, Any] | None,
    turn_error_code: Any = None,
    turn_error_text: Any = None,
    code_version: Any = None,
    git_commit: Any = None,
    sensitive_identity_values: Sequence[str] = (),
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build a portable projection without re-deciding any effect outcome."""

    redaction_counter = [0]
    truncation_counter = [0]

    def scalar(value: Any, limit: int = _HANDLE_LIMIT) -> str | None:
        return _bounded_scalar(
            value,
            limit=limit,
            sensitive_identity_values=sensitive_identity_values,
            redaction_counter=redaction_counter,
            truncation_counter=truncation_counter,
        )

    report = (
        outcome_report
        if isinstance(outcome_report, Mapping)
        and outcome_report.get("schema_version")
        == "adaptive_turn_effect_outcome_report.v1"
        else None
    )
    raw_facts = report.get("facts") if isinstance(report, Mapping) else []
    indexed_facts = [
        (index, fact)
        for index, fact in enumerate(raw_facts if isinstance(raw_facts, list) else [])
        if isinstance(fact, Mapping)
    ]
    ordered_facts = [fact for _index, fact in sorted(indexed_facts, key=_effect_priority)]
    effects = [
        effect
        for fact in ordered_facts[:_MAX_EFFECTS]
        if (
            effect := _project_effect(
                fact,
                sensitive_identity_values=sensitive_identity_values,
                redaction_counter=redaction_counter,
                truncation_counter=truncation_counter,
            )
        )
        is not None
    ]

    scope_modes: list[str] = []
    raw_scopes = report.get("canonical_scopes") if isinstance(report, Mapping) else []
    if isinstance(raw_scopes, list):
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, Mapping):
                continue
            mode = scalar(raw_scope.get("mode"), _STATUS_LIMIT)
            if mode in {"user", "organisation", "global"} and mode not in scope_modes:
                scope_modes.append(mode)

    visible_surface = _text_surface(
        visible_response,
        limit=_VISIBLE_RESPONSE_LIMIT,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
    )
    draft_value: Any = None
    if isinstance(report, Mapping):
        raw_draft = report.get("model_draft")
        if isinstance(raw_draft, Mapping):
            draft_value = raw_draft.get("preview")
        else:
            draft_value = raw_draft
    draft_surface = _text_surface(
        draft_value,
        limit=_DRAFT_LIMIT,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
    )
    if draft_surface is not None:
        draft_surface["authority"] = "non_authoritative"

    turn_error = _build_turn_error(
        error_code=turn_error_code,
        error_text=turn_error_text,
        sensitive_identity_values=sensitive_identity_values,
        redaction_counter=redaction_counter,
        truncation_counter=truncation_counter,
    )
    total_count = len(indexed_facts)
    initial_omitted_count = max(0, total_count - len(effects))
    producer = {
        key: value
        for key, value in (
            ("code_version", scalar(code_version, _HANDLE_LIMIT)),
            ("git_commit", scalar(git_commit, _HANDLE_LIMIT)),
        )
        if value is not None
    }
    capsule: dict[str, Any] = {
        "schema_version": TURN_FAILURE_CAPSULE_SCHEMA_VERSION,
        "generated_at_utc": scalar(generated_at_utc or _utc_now_iso(), _HANDLE_LIMIT),
        "request_id": scalar(request_id, _HANDLE_LIMIT),
        "terminal_status": scalar(terminal_status, _STATUS_LIMIT),
        "response_authority": (
            scalar(response_authority, _STATUS_LIMIT) or "not_recorded"
        ),
        "producer": producer,
        "effects": effects,
        "effect_summary": {
            "total_count": total_count,
            "included_count": len(effects),
            "omitted_count": initial_omitted_count,
        },
        "canonical_scope_modes": scope_modes,
        "redaction": {
            "applied": redaction_counter[0] > 0,
            "redacted_count": redaction_counter[0],
            "truncated": bool(
                truncation_counter[0]
                or initial_omitted_count
                or (visible_surface and visible_surface.get("truncated"))
                or (draft_surface and draft_surface.get("truncated"))
                or any(
                    isinstance(effect.get("error"), Mapping)
                    and isinstance(effect["error"].get("preview"), Mapping)
                    and effect["error"]["preview"].get("truncated") is True
                    for effect in effects
                )
            ),
        },
    }
    for key, value in (
        ("visible_response", visible_surface),
        ("pre_presentation_draft", draft_surface),
        ("turn_error", turn_error),
    ):
        if value is not None:
            capsule[key] = value
    capsule = {key: value for key, value in capsule.items() if value is not None}
    _enforce_hard_bound(capsule)
    return capsule


def turn_failure_capsule_size_bytes(capsule: Mapping[str, Any]) -> int:
    return len(_compact_json_bytes(capsule))


def project_stored_turn_failure_capsule(
    value: Any,
) -> dict[str, Any] | None:
    """Fail-closed allowlist for serving an already-produced capsule.

    This is deliberately not a repair or recomputation path. It preserves the
    stored producer, hashes, text projections, and outcome fields verbatim while
    dropping fields outside the portable schema. Invalid required structure is
    unavailable rather than partially reinterpreted.
    """

    if not isinstance(value, Mapping):
        return None
    if value.get("schema_version") != TURN_FAILURE_CAPSULE_SCHEMA_VERSION:
        return None

    def required_string(raw: Any, limit: int) -> str | None:
        return raw if isinstance(raw, str) and 0 < len(raw) <= limit else None

    def optional_string(raw: Any, limit: int) -> tuple[bool, str | None]:
        if raw is None:
            return True, None
        if not isinstance(raw, str) or not raw or len(raw) > limit:
            return False, None
        return True, raw

    def text_surface(
        raw: Any, *, allow_authority: bool = False
    ) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        text = raw.get("text")
        char_count = raw.get("char_count")
        sha256 = raw.get("sha256")
        truncated = raw.get("truncated")
        if (
            not isinstance(text, str)
            or len(text) > _VISIBLE_RESPONSE_LIMIT
            or not isinstance(char_count, int)
            or isinstance(char_count, bool)
            or char_count < len(text)
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or not isinstance(truncated, bool)
        ):
            return None
        projected = {
            "text": text,
            "char_count": char_count,
            "sha256": sha256,
            "truncated": truncated,
        }
        if allow_authority:
            if raw.get("authority") != "non_authoritative":
                return None
            projected["authority"] = "non_authoritative"
        return projected

    request_id = required_string(value.get("request_id"), _HANDLE_LIMIT)
    generated_at_utc = required_string(
        value.get("generated_at_utc"), _HANDLE_LIMIT
    )
    terminal_status = required_string(value.get("terminal_status"), _STATUS_LIMIT)
    response_authority = required_string(
        value.get("response_authority"), _STATUS_LIMIT
    )
    if not all((request_id, generated_at_utc, terminal_status, response_authority)):
        return None

    producer_raw = value.get("producer")
    if not isinstance(producer_raw, Mapping):
        return None
    producer: dict[str, str] = {}
    for key in ("code_version", "git_commit"):
        valid, projected = optional_string(producer_raw.get(key), _HANDLE_LIMIT)
        if not valid:
            return None
        if projected is not None:
            producer[key] = projected

    effects_raw = value.get("effects")
    if not isinstance(effects_raw, list) or len(effects_raw) > _MAX_EFFECTS:
        return None
    effects: list[dict[str, Any]] = []
    string_fields = {
        "effect_id": _HANDLE_LIMIT,
        "name": _NAME_LIMIT,
        "initial_status": _STATUS_LIMIT,
        "status": _STATUS_LIMIT,
        "current_outcome_status": _STATUS_LIMIT,
        "reconciliation_status": _STATUS_LIMIT,
        "canonical_readback_verdict": _STATUS_LIMIT,
        "recovered_by_effect_id": _HANDLE_LIMIT,
        "workflow_id": _HANDLE_LIMIT,
        "instance_id": _HANDLE_LIMIT,
        "evidence_id": _HANDLE_LIMIT,
    }
    for raw_effect in effects_raw:
        if not isinstance(raw_effect, Mapping):
            return None
        effect: dict[str, Any] = {}
        for key, limit in string_fields.items():
            valid, projected = optional_string(raw_effect.get(key), limit)
            if not valid:
                return None
            if projected is not None:
                effect[key] = projected
        if not any(key in effect for key in ("effect_id", "name", "status")):
            return None
        for key in ("changed", "outcome_resolved"):
            raw_boolean = raw_effect.get(key)
            if raw_boolean is not None and not isinstance(raw_boolean, bool):
                return None
            if isinstance(raw_boolean, bool):
                effect[key] = raw_boolean
        raw_error = raw_effect.get("error")
        if raw_error is not None:
            if not isinstance(raw_error, Mapping):
                return None
            error: dict[str, Any] = {}
            valid, error_code = optional_string(
                raw_error.get("code"), _ERROR_CODE_LIMIT
            )
            if not valid:
                return None
            if error_code is not None:
                error["code"] = error_code
            if raw_error.get("preview") is not None:
                preview = text_surface(raw_error.get("preview"))
                if preview is None:
                    return None
                error["preview"] = preview
            if not error:
                return None
            effect["error"] = error
        effects.append(effect)

    summary_raw = value.get("effect_summary")
    if not isinstance(summary_raw, Mapping):
        return None
    summary_values: dict[str, int] = {}
    for key in ("total_count", "included_count", "omitted_count"):
        raw_count = summary_raw.get(key)
        if (
            not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count < 0
        ):
            return None
        summary_values[key] = raw_count
    if (
        summary_values["included_count"] != len(effects)
        or summary_values["total_count"]
        != summary_values["included_count"] + summary_values["omitted_count"]
    ):
        return None

    raw_scope_modes = value.get("canonical_scope_modes")
    if not isinstance(raw_scope_modes, list) or len(raw_scope_modes) > 3:
        return None
    scope_modes: list[str] = []
    for raw_mode in raw_scope_modes:
        if (
            raw_mode not in {"user", "organisation", "global"}
            or raw_mode in scope_modes
        ):
            return None
        scope_modes.append(raw_mode)

    redaction_raw = value.get("redaction")
    if not isinstance(redaction_raw, Mapping):
        return None
    applied = redaction_raw.get("applied")
    redacted_count = redaction_raw.get("redacted_count")
    truncated = redaction_raw.get("truncated")
    if (
        not isinstance(applied, bool)
        or not isinstance(redacted_count, int)
        or isinstance(redacted_count, bool)
        or redacted_count < 0
        or not isinstance(truncated, bool)
    ):
        return None

    projected_capsule: dict[str, Any] = {
        "schema_version": TURN_FAILURE_CAPSULE_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc,
        "request_id": request_id,
        "terminal_status": terminal_status,
        "response_authority": response_authority,
        "producer": producer,
        "effects": effects,
        "effect_summary": summary_values,
        "canonical_scope_modes": scope_modes,
        "redaction": {
            "applied": applied,
            "redacted_count": redacted_count,
            "truncated": truncated,
        },
    }
    for key, allow_authority in (
        ("visible_response", False),
        ("pre_presentation_draft", True),
    ):
        if value.get(key) is None:
            continue
        projected_surface = text_surface(
            value.get(key), allow_authority=allow_authority
        )
        if projected_surface is None:
            return None
        projected_capsule[key] = projected_surface

    raw_turn_error = value.get("turn_error")
    if raw_turn_error is not None:
        if not isinstance(raw_turn_error, Mapping):
            return None
        turn_error: dict[str, Any] = {}
        valid, code = optional_string(raw_turn_error.get("code"), _ERROR_CODE_LIMIT)
        if not valid:
            return None
        if code is not None:
            turn_error["code"] = code
        if raw_turn_error.get("preview") is not None:
            preview = text_surface(raw_turn_error.get("preview"))
            if preview is None:
                return None
            turn_error["preview"] = preview
        if not turn_error:
            return None
        projected_capsule["turn_error"] = turn_error
    return projected_capsule


__all__ = [
    "TURN_FAILURE_CAPSULE_MAX_BYTES",
    "TURN_FAILURE_CAPSULE_SCHEMA_VERSION",
    "build_turn_failure_capsule",
    "project_stored_turn_failure_capsule",
    "turn_failure_capsule_size_bytes",
]
