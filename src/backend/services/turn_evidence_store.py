"""Turn-scoped storage and bounded hydration for tool evidence.

The store preserves complete tool results for the lifetime of one ordinary
turn while exposing only compact, provenance-bearing envelopes to a model.
Evidence identifiers are opaque and can be resolved only with the same trusted
actor scope and turn identifier that created the store.

This module owns no routing, retry, or domain policy.  Callers remain free to
offer any authorised tools and to let the model choose how and when to inspect
stored evidence.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EVIDENCE_ENVELOPE_SCHEMA_VERSION = "turn_evidence_envelope.v1"
EVIDENCE_SLICE_SCHEMA_VERSION = "turn_evidence_slice.v1"
DEFAULT_PREVIEW_MAX_CHARS = 1_200
DEFAULT_READ_MAX_CHARS = 4_000
MAX_READ_MAX_CHARS = 16_000

_MAX_SHAPE_KEYS = 20
_MAX_SHAPE_KEY_CHARS = 120
_MAX_PROVENANCE_FIELDS = 20
_MAX_PROVENANCE_STRING_CHARS = 300
_MAX_QUERY_SNIPPET_CHARS = 240
_MAX_QUERY_POINTER_CHARS = 500


def _clean_optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class TrustedTurnScope:
    """Server-derived actor scope used to bind ephemeral turn evidence."""

    user_concept_id: str | None = None
    organisation_concept_id: str | None = None
    namespace: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "user_concept_id",
            _clean_optional_text(self.user_concept_id),
        )
        object.__setattr__(
            self,
            "organisation_concept_id",
            _clean_optional_text(self.organisation_concept_id),
        )
        object.__setattr__(self, "namespace", _clean_optional_text(self.namespace))


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Compact model-visible locator for one complete stored tool result."""

    evidence_id: str
    tool_name: str
    call_id: str
    turn_id: str
    status: str
    sha256: str
    size_bytes: int
    char_count: int
    content_type: str
    value_kind: str
    shape: Mapping[str, Any]
    preview: str
    preview_format: str
    preview_truncated: bool
    provenance: Mapping[str, Any]
    available_selectors: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_ENVELOPE_SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "tool_name": self.tool_name,
            "call_id": self.call_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "trust_boundary": "untrusted_tool_output",
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "char_count": self.char_count,
            "content_type": self.content_type,
            "value_kind": self.value_kind,
            "shape": copy.deepcopy(dict(self.shape)),
            "preview": self.preview,
            "preview_format": self.preview_format,
            "preview_truncated": self.preview_truncated,
            "provenance": copy.deepcopy(dict(self.provenance)),
            "available_selectors": list(self.available_selectors),
        }


@dataclass(frozen=True)
class _StoredEvidence:
    envelope: EvidenceEnvelope
    value: Any


def _normalise_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "bytes_base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, bytearray):
        return _normalise_json_value(bytes(value))
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_json_value(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_normalise_json_value(item) for item in value]
    return str(value)


def _render_value(value: Any) -> tuple[bytes, str, str, str]:
    """Return canonical bytes, readable text, content type, and format."""

    if isinstance(value, str):
        raw_bytes = value.encode("utf-8", errors="replace")
        return raw_bytes, value, "text/plain", "text"
    if isinstance(value, (bytes, bytearray)):
        raw_bytes = bytes(value)
        rendered = base64.b64encode(raw_bytes).decode("ascii")
        return raw_bytes, rendered, "application/octet-stream", "base64"

    try:
        rendered = json.dumps(
            _normalise_json_value(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw_bytes = rendered.encode("utf-8")
        return raw_bytes, rendered, "application/json", "json"
    except (RecursionError, TypeError, ValueError):
        rendered = str(value)
        raw_bytes = rendered.encode("utf-8", errors="replace")
        return raw_bytes, rendered, "text/plain", "text"


def _value_kind(value: Any) -> str:
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (bytes, bytearray)):
        return "bytes"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _shape(value: Any) -> dict[str, Any]:
    kind = _value_kind(value)
    if isinstance(value, Mapping):
        keys = [str(key)[:_MAX_SHAPE_KEY_CHARS] for key in value]
        return {
            "type": kind,
            "key_count": len(value),
            "keys": keys[:_MAX_SHAPE_KEYS],
            "keys_omitted_count": max(0, len(keys) - _MAX_SHAPE_KEYS),
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        sampled_types = sorted({_value_kind(item) for item in value[:20]})
        return {
            "type": kind,
            "item_count": len(value),
            "sampled_item_types": sampled_types,
        }
    if isinstance(value, str):
        return {"type": kind, "char_count": len(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"type": kind, "byte_count": len(value)}
    return {"type": kind}


def _compact_provenance(
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(provenance, Mapping):
        return {}

    compacted: dict[str, Any] = {}
    for raw_key, value in list(provenance.items())[:_MAX_PROVENANCE_FIELDS]:
        key = str(raw_key)[:_MAX_SHAPE_KEY_CHARS]
        if value is None or isinstance(value, (bool, int, float)):
            compacted[key] = value
        elif isinstance(value, str):
            compacted[key] = value[:_MAX_PROVENANCE_STRING_CHARS]
            if len(value) > _MAX_PROVENANCE_STRING_CHARS:
                compacted[f"{key}_truncated"] = True
        else:
            compacted[key] = _shape(value)
    omitted = max(0, len(provenance) - _MAX_PROVENANCE_FIELDS)
    if omitted:
        compacted["_omitted_field_count"] = omitted
    return compacted


def _escape_json_pointer_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _unescape_json_pointer_token(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _mapping_item_for_pointer(value: Mapping[Any, Any], token: str) -> Any:
    if token in value:
        return value[token]
    for key, item in value.items():
        if str(key) == token:
            return item
    raise KeyError(token)


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with '/'.")

    current = value
    for raw_token in pointer[1:].split("/"):
        token = _unescape_json_pointer_token(raw_token)
        if isinstance(current, Mapping):
            current = _mapping_item_for_pointer(current, token)
            continue
        if isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            if token == "-":
                raise KeyError(token)
            try:
                index = int(token)
            except (TypeError, ValueError) as exc:
                raise KeyError(token) from exc
            if index < 0 or index >= len(current):
                raise KeyError(token)
            current = current[index]
            continue
        raise KeyError(token)
    return current


def _query_snippet(text: str, query: str) -> tuple[str, int]:
    match_start = text.lower().find(query.lower())
    if match_start < 0:
        return "", -1
    half_window = max(1, _MAX_QUERY_SNIPPET_CHARS // 2)
    snippet_start = max(0, match_start - half_window)
    snippet_end = min(len(text), snippet_start + _MAX_QUERY_SNIPPET_CHARS)
    return text[snippet_start:snippet_end], match_start


def _iter_query_matches(
    value: Any,
    query: str,
    *,
    pointer: str = "",
    depth: int = 0,
) -> Iterator[dict[str, Any]]:
    if depth > 64:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            token = _escape_json_pointer_token(key)
            item_pointer = f"{pointer}/{token}"
            key_text = str(key)
            if query.lower() in key_text.lower():
                yield {
                    "json_pointer": item_pointer[:_MAX_QUERY_POINTER_CHARS],
                    "match_kind": "key",
                    "snippet": key_text[:_MAX_QUERY_SNIPPET_CHARS],
                    "match_start": key_text.lower().find(query.lower()),
                }
            yield from _iter_query_matches(
                item,
                query,
                pointer=item_pointer,
                depth=depth + 1,
            )
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for index, item in enumerate(value):
            yield from _iter_query_matches(
                item,
                query,
                pointer=f"{pointer}/{index}",
                depth=depth + 1,
            )
        return

    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="replace")
    elif value is None:
        return
    else:
        text = str(value)
    snippet, match_start = _query_snippet(text, query)
    if match_start >= 0:
        yield {
            "json_pointer": pointer[:_MAX_QUERY_POINTER_CHARS],
            "match_kind": "value",
            "snippet": snippet,
            "match_start": match_start,
        }


def _bounded_positive_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class TurnEvidenceStore:
    """Preserve complete tool results behind actor- and turn-bound handles."""

    def __init__(self, scope: TrustedTurnScope, turn_id: str) -> None:
        if not isinstance(scope, TrustedTurnScope):
            raise TypeError("scope must be a TrustedTurnScope")
        clean_turn_id = _clean_optional_text(turn_id)
        if clean_turn_id is None:
            raise ValueError("turn_id must be a non-empty string")
        self.scope = scope
        self.turn_id = clean_turn_id
        self._records: dict[str, _StoredEvidence] = {}
        self._record_order: list[str] = []
        self._lock = threading.RLock()

    def record(
        self,
        tool_name: str,
        call_id: str,
        value: Any,
        provenance: Mapping[str, Any] | None = None,
        status: str = "ok",
    ) -> EvidenceEnvelope:
        """Store one result and return its compact model-visible envelope."""

        clean_tool_name = _clean_optional_text(tool_name)
        clean_call_id = _clean_optional_text(call_id)
        clean_status = _clean_optional_text(status)
        if clean_tool_name is None:
            raise ValueError("tool_name must be a non-empty string")
        if clean_call_id is None:
            raise ValueError("call_id must be a non-empty string")
        if clean_status is None:
            raise ValueError("status must be a non-empty string")

        try:
            stored_value = copy.deepcopy(value)
        # Tool results may contain integration-owned objects whose custom
        # deepcopy hooks raise arbitrary exceptions. Rendering remains bounded
        # even when defensive copying is unavailable.
        except Exception:  # noqa: BLE001
            stored_value = value
        raw_bytes, rendered, content_type, preview_format = _render_value(
            stored_value
        )
        preview = rendered[:DEFAULT_PREVIEW_MAX_CHARS]
        value_kind = _value_kind(stored_value)
        selectors = ["offset", "query"]
        if isinstance(stored_value, (Mapping, list, tuple)):
            selectors.insert(0, "json_pointer")

        with self._lock:
            while True:
                evidence_id = f"ev_{secrets.token_urlsafe(18)}"
                if evidence_id not in self._records:
                    break
            envelope = EvidenceEnvelope(
                evidence_id=evidence_id,
                tool_name=clean_tool_name,
                call_id=clean_call_id,
                turn_id=self.turn_id,
                status=clean_status,
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                size_bytes=len(raw_bytes),
                char_count=len(rendered),
                content_type=content_type,
                value_kind=value_kind,
                shape=_shape(stored_value),
                preview=preview,
                preview_format=preview_format,
                preview_truncated=len(rendered) > len(preview),
                provenance=_compact_provenance(provenance),
                available_selectors=tuple(selectors),
            )
            self._records[evidence_id] = _StoredEvidence(
                envelope=envelope,
                value=stored_value,
            )
            self._record_order.append(evidence_id)
        return envelope

    def index(self) -> list[dict[str, Any]]:
        """Return the compact evidence envelopes in insertion order."""

        with self._lock:
            return [
                self._records[evidence_id].envelope.to_mapping()
                for evidence_id in self._record_order
            ]

    def _authorised_record(
        self,
        evidence_id: str,
        *,
        trusted_scope: TrustedTurnScope,
        turn_id: str,
    ) -> _StoredEvidence | None:
        if (
            not isinstance(trusted_scope, TrustedTurnScope)
            or trusted_scope != self.scope
            or _clean_optional_text(turn_id) != self.turn_id
        ):
            return None
        clean_evidence_id = _clean_optional_text(evidence_id)
        if clean_evidence_id is None:
            return None
        with self._lock:
            return self._records.get(clean_evidence_id)

    @staticmethod
    def _not_found(evidence_id: Any) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SLICE_SCHEMA_VERSION,
            "success": False,
            "error_code": "evidence_not_found",
            "evidence_id": (
                evidence_id.strip()
                if isinstance(evidence_id, str) and evidence_id.strip()
                else None
            ),
        }

    @staticmethod
    def _slice_metadata(record: _StoredEvidence) -> dict[str, Any]:
        envelope = record.envelope
        return {
            "schema_version": EVIDENCE_SLICE_SCHEMA_VERSION,
            "success": True,
            "evidence_id": envelope.evidence_id,
            "tool_name": envelope.tool_name,
            "call_id": envelope.call_id,
            "turn_id": envelope.turn_id,
            "status": envelope.status,
            "trust_boundary": "untrusted_tool_output",
            "source_sha256": envelope.sha256,
            "source_size_bytes": envelope.size_bytes,
            "provenance": copy.deepcopy(dict(envelope.provenance)),
        }

    def _read_query(
        self,
        record: _StoredEvidence,
        *,
        value: Any,
        query: str,
        json_pointer: str | None,
        offset: int,
        max_chars: int,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        has_more = False
        skipped = 0
        for match in _iter_query_matches(
            value,
            query,
            pointer=json_pointer or "",
        ):
            if skipped < offset:
                skipped += 1
                continue
            candidate_matches = [*matches, match]
            rendered = json.dumps(
                candidate_matches,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            if len(rendered) > max_chars:
                has_more = True
                break
            matches.append(match)

        result = self._slice_metadata(record)
        result.update(
            {
                "selector": {
                    "json_pointer": json_pointer,
                    "query": query,
                    "offset": offset,
                    "max_chars": max_chars,
                },
                "content_format": "query_matches",
                "matches": matches,
                "returned_match_count": len(matches),
                "returned_chars": len(
                    json.dumps(matches, ensure_ascii=True, separators=(",", ":"))
                ),
                "has_more": has_more,
                "next_offset": offset + len(matches) if has_more else None,
            }
        )
        return result

    def read(
        self,
        evidence_id: str,
        *,
        json_pointer: str | None = None,
        query: str | None = None,
        offset: int = 0,
        max_chars: int = DEFAULT_READ_MAX_CHARS,
        trusted_scope: TrustedTurnScope,
        turn_id: str,
    ) -> dict[str, Any]:
        """Read one bounded slice or search view of stored evidence."""

        record = self._authorised_record(
            evidence_id,
            trusted_scope=trusted_scope,
            turn_id=turn_id,
        )
        if record is None:
            return self._not_found(evidence_id)

        clean_pointer = (
            json_pointer if isinstance(json_pointer, str) else None
        )
        clean_query = _clean_optional_text(query)

        bounded_offset = _bounded_positive_int(
            offset,
            default=0,
            minimum=0,
            maximum=2_000_000_000,
        )
        bounded_max_chars = _bounded_positive_int(
            max_chars,
            default=DEFAULT_READ_MAX_CHARS,
            minimum=1,
            maximum=MAX_READ_MAX_CHARS,
        )
        selected_value = record.value
        if clean_pointer is not None:
            try:
                selected_value = _resolve_json_pointer(
                    selected_value,
                    clean_pointer,
                )
            except ValueError as exc:
                return {
                    **self._slice_metadata(record),
                    "success": False,
                    "error_code": "invalid_json_pointer",
                    "message": str(exc),
                    "json_pointer": clean_pointer,
                }
            except KeyError:
                return {
                    **self._slice_metadata(record),
                    "success": False,
                    "error_code": "evidence_path_not_found",
                    "json_pointer": clean_pointer,
                }

        if clean_query is not None:
            return self._read_query(
                record,
                value=selected_value,
                query=clean_query,
                json_pointer=clean_pointer,
                offset=bounded_offset,
                max_chars=bounded_max_chars,
            )

        _raw_bytes, rendered, _content_type, content_format = _render_value(
            selected_value
        )
        content = rendered[bounded_offset : bounded_offset + bounded_max_chars]
        next_offset = bounded_offset + len(content)
        has_more = next_offset < len(rendered)
        result = self._slice_metadata(record)
        result.update(
            {
                "selector": {
                    "json_pointer": clean_pointer,
                    "offset": bounded_offset,
                    "max_chars": bounded_max_chars,
                },
                "content": content,
                "content_format": content_format,
                "selected_value_kind": _value_kind(selected_value),
                "selected_value_shape": _shape(selected_value),
                "total_chars": len(rendered),
                "returned_chars": len(content),
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
            }
        )
        return result


__all__ = [
    "DEFAULT_PREVIEW_MAX_CHARS",
    "DEFAULT_READ_MAX_CHARS",
    "EVIDENCE_ENVELOPE_SCHEMA_VERSION",
    "EVIDENCE_SLICE_SCHEMA_VERSION",
    "MAX_READ_MAX_CHARS",
    "EvidenceEnvelope",
    "TrustedTurnScope",
    "TurnEvidenceStore",
]
