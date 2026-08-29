"""Parse and import external agent transcripts as actor-scoped conversations.

The vendor file remains the authoritative source artefact.  This module builds a
versioned, deterministic event package and projects only ordinary user-visible
human/assistant text into chat history.  System instructions, reasoning, tool
calls, and tool results are retained in the raw artefact and counted in the
projection report; they never become current Von instructions or authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import chat_history_service, concept_service
from .ai_chat_session_ingestion_service import (
    AIChatSessionIngestionService,
    SessionRecord,
)

logger = logging.getLogger(__name__)


EXTERNAL_CONVERSATION_PACKAGE_SCHEMA_VERSION = "external_conversation_package.v1"
EXTERNAL_CONVERSATION_PARSER_VERSION = "2026-08-29.2"
EXTERNAL_CONVERSATION_ORIGIN_KIND = "external_conversation_import"
EXTERNAL_CONVERSATION_LINEAGE_SCHEMA_VERSION = "conversation_lineage.v1"
MAX_EXTERNAL_CONVERSATION_SOURCE_BYTES = 32 * 1024 * 1024
MAX_EXTERNAL_CONVERSATION_RECORDS = 100_000
MAX_STREAMED_EXTERNAL_CONVERSATION_RECORDS = 1_000_000
MAX_EXTERNAL_CONVERSATION_JSONL_LINE_BYTES = 8 * 1024 * 1024
MAX_PROJECTED_MESSAGE_CHARS = 250_000
MAX_PROJECTED_TOTAL_CHARS = 4_000_000
MAX_PROJECTED_MESSAGES = 20_000


class ExternalConversationImportError(ValueError):
    """A source artefact cannot be safely interpreted or imported."""

    def __init__(
        self, message: str, *, error_code: str = "invalid_external_conversation"
    ):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class ExternalActor:
    source_actor_id: str
    actor_kind: str
    display_name: str | None = None


@dataclass(frozen=True)
class ExternalContentBlock:
    block_kind: str
    text: str | None = None
    source_type: str | None = None


@dataclass(frozen=True)
class ExternalConversationEvent:
    event_id: str
    event_kind: str
    source_role: str | None
    actor: ExternalActor
    content_blocks: tuple[ExternalContentBlock, ...]
    source_timestamp_utc: str | None
    raw_locator: str
    parent_event_id: str | None = None
    branch_id: str | None = None
    model: str | None = None
    user_visible: bool = False

    @property
    def visible_text(self) -> str:
        return "\n\n".join(
            block.text.strip()
            for block in self.content_blocks
            if block.block_kind == "text"
            and isinstance(block.text, str)
            and block.text.strip()
        ).strip()


@dataclass
class ExternalConversationPackage:
    provider: str
    source_session_id: str
    source_title: str | None
    source_account: str | None
    source_workspace: str | None
    source_format: str
    source_sha256: str
    parser_id: str
    parser_version: str
    events: list[ExternalConversationEvent]
    participants: list[ExternalActor]
    source_created_at_utc: str | None = None
    source_updated_at_utc: str | None = None
    parent_conversation_id: str | None = None
    loss_report: dict[str, Any] = field(default_factory=dict)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": EXTERNAL_CONVERSATION_PACKAGE_SCHEMA_VERSION,
            "provider": self.provider,
            "source_session_id": self.source_session_id,
            "source_title": self.source_title,
            "source_account": self.source_account,
            "source_workspace": self.source_workspace,
            "source_format": self.source_format,
            "source_sha256": self.source_sha256,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "source_created_at_utc": self.source_created_at_utc,
            "source_updated_at_utc": self.source_updated_at_utc,
            "parent_conversation_id": self.parent_conversation_id,
            "participants": [asdict(actor) for actor in self.participants],
            "events": [asdict(event) for event in self.events],
            "loss_report": dict(self.loss_report),
        }

    @property
    def package_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _clean_text(value: Any, *, maximum: int | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if isinstance(maximum, int) and maximum > 0:
        cleaned = cleaned[:maximum]
    return cleaned or None


def _normalise_provider(value: Any) -> str | None:
    cleaned = _clean_text(value, maximum=80)
    if not cleaned or cleaned.lower() in {"auto", "detect"}:
        return None
    key = re.sub(r"[^a-z0-9]+", "_", cleaned.lower()).strip("_")
    aliases = {
        "claude": "claude_code",
        "anthropic": "claude_code",
        "claude_code": "claude_code",
        "github_copilot": "copilot",
        "vscode": "copilot",
        "vs_code": "copilot",
        "copilot": "copilot",
        "codex": "codex",
        "openai_codex": "codex",
        "gemini": "gemini",
        "google_gemini": "gemini",
    }
    provider = aliases.get(key)
    if provider is None:
        raise ExternalConversationImportError(
            f"Unsupported provider hint: {cleaned}",
            error_code="unsupported_external_conversation_provider",
        )
    return provider


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = _clean_text(value, maximum=120)
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _stable_event_id(
    *,
    provider: str,
    source_session_id: str,
    raw_locator: str,
    event_kind: str,
    source_id: Any = None,
) -> str:
    explicit = _clean_text(source_id, maximum=240)
    if explicit:
        return f"{provider}:{explicit}"
    digest = hashlib.sha256(
        f"{provider}|{source_session_id}|{raw_locator}|{event_kind}".encode("utf-8")
    ).hexdigest()
    return f"{provider}:event:{digest[:24]}"


def stable_imported_conversation_session_id(
    *,
    custodian_user_id: str,
    provider: str,
    source_session_id: str,
    namespace: str | None = None,
    source_account: str | None = None,
    source_workspace: str | None = None,
) -> str:
    identity = "|".join(
        [
            custodian_user_id.strip(),
            (namespace or "").strip(),
            provider.strip().lower(),
            (source_account or "").strip().lower(),
            (source_workspace or "").strip().lower(),
            source_session_id.strip(),
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"external-{provider}-{digest[:24]}"


def _text_blocks(value: Any) -> tuple[ExternalContentBlock, ...]:
    blocks: list[ExternalContentBlock] = []
    if isinstance(value, str):
        if value.strip():
            blocks.append(ExternalContentBlock(block_kind="text", text=value))
        return tuple(blocks)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    for item in value:
        if isinstance(item, str):
            if item.strip():
                blocks.append(ExternalContentBlock(block_kind="text", text=item))
            continue
        if not isinstance(item, Mapping):
            continue
        source_type = _clean_text(item.get("type"), maximum=80)
        if source_type is None and isinstance(item.get("text"), str):
            source_type = "text"
        source_type = source_type or "unknown"
        if source_type.lower() not in {
            "text",
            "input_text",
            "output_text",
            "markdown",
            "markdown_content",
        }:
            continue
        text = _clean_text(item.get("text") or item.get("value") or item.get("content"))
        if text:
            blocks.append(
                ExternalContentBlock(
                    block_kind="text",
                    text=text,
                    source_type=source_type,
                )
            )
    return tuple(blocks)


def _actor_for_role(provider: str, role: str | None) -> ExternalActor:
    if role == "user":
        return ExternalActor(
            source_actor_id=f"{provider}:source_user",
            actor_kind="unresolved_human",
            display_name="Source user",
        )
    if role == "assistant":
        return ExternalActor(
            source_actor_id=f"{provider}:agent",
            actor_kind="agent",
            display_name={
                "codex": "Codex",
                "claude_code": "Claude Code",
                "copilot": "GitHub Copilot",
                "gemini": "Gemini",
            }.get(provider, provider),
        )
    if role == "tool":
        return ExternalActor(
            source_actor_id=f"{provider}:tool",
            actor_kind="tool",
            display_name="Tool",
        )
    return ExternalActor(
        source_actor_id=f"{provider}:{role or 'system'}",
        actor_kind="system",
        display_name=(role or "System").title(),
    )


def _dedupe_participants(
    events: Iterable[ExternalConversationEvent],
) -> list[ExternalActor]:
    participants: dict[str, ExternalActor] = {}
    for event in events:
        participants.setdefault(event.actor.source_actor_id, event.actor)
    return list(participants.values())


def _package_title(events: Sequence[ExternalConversationEvent]) -> str | None:
    for event in events:
        if event.source_role != "user" or not event.user_visible:
            continue
        text = event.visible_text
        if text:
            compact = re.sub(r"\s+", " ", text).strip()
            return compact[:80]
    return None


def _finish_package(
    *,
    provider: str,
    source_session_id: str,
    source_title: str | None,
    source_account: str | None,
    source_workspace: str | None,
    source_format: str,
    source_sha256: str,
    parser_id: str,
    events: list[ExternalConversationEvent],
    parent_conversation_id: str | None = None,
    loss_report: Mapping[str, Any] | None = None,
) -> ExternalConversationPackage:
    if not events:
        raise ExternalConversationImportError(
            "The source did not contain any supported conversation events.",
            error_code="external_conversation_has_no_supported_events",
        )
    timestamps = [
        value
        for event in events
        for value in [event.source_timestamp_utc]
        if value is not None
    ]
    visible_count = sum(
        bool(event.user_visible and event.visible_text) for event in events
    )
    report = dict(loss_report or {})
    report.update(
        {
            "total_event_count": len(events),
            "user_visible_event_count": visible_count,
            "non_projected_event_count": max(0, len(events) - visible_count),
        }
    )
    return ExternalConversationPackage(
        provider=provider,
        source_session_id=source_session_id,
        source_title=_clean_text(source_title, maximum=160) or _package_title(events),
        source_account=_clean_text(source_account, maximum=240),
        source_workspace=_clean_text(source_workspace, maximum=500),
        source_format=source_format,
        source_sha256=source_sha256,
        parser_id=parser_id,
        parser_version=EXTERNAL_CONVERSATION_PARSER_VERSION,
        events=events,
        participants=_dedupe_participants(events),
        source_created_at_utc=min(timestamps) if timestamps else None,
        source_updated_at_utc=max(timestamps) if timestamps else None,
        parent_conversation_id=_clean_text(parent_conversation_id, maximum=240),
        loss_report=report,
    )


def _parse_json_lines(text: str) -> tuple[list[Mapping[str, Any]], int]:
    records: list[Mapping[str, Any]] = []
    invalid = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(records) + invalid >= MAX_EXTERNAL_CONVERSATION_RECORDS:
            raise ExternalConversationImportError(
                "The source contains too many records.",
                error_code="external_conversation_record_limit_exceeded",
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(value, Mapping):
            record = dict(value)
            record["__line_number"] = line_number
            records.append(record)
        else:
            invalid += 1
    return records, invalid


def _detect_provider(text: str, provider_hint: str | None) -> tuple[str, Any, str]:
    if provider_hint:
        provider = provider_hint
    else:
        provider = ""
    stripped = text.lstrip()
    parsed_json: Any = None
    source_format = "jsonl"
    if stripped.startswith(("{", "[")):
        try:
            parsed_json = json.loads(text)
            source_format = "json"
        except json.JSONDecodeError:
            parsed_json = None

    sample_records: list[Mapping[str, Any]] = []
    if isinstance(parsed_json, Mapping):
        sample_records = [parsed_json]
    elif isinstance(parsed_json, list):
        sample_records = [
            item for item in parsed_json[:20] if isinstance(item, Mapping)
        ]
    else:
        sample_records, _invalid = _parse_json_lines(text)
        sample_records = sample_records[:20]

    if not provider:
        if any(
            record.get("type") in {"session_meta", "event_msg", "response_item"}
            and isinstance(record.get("payload"), Mapping)
            for record in sample_records
        ):
            provider = "codex"
        elif any(
            record.get("type") in {"user", "assistant", "system", "ai-title"}
            and ("sessionId" in record or "uuid" in record)
            for record in sample_records
        ):
            provider = "claude_code"
        elif (
            isinstance(parsed_json, Mapping)
            and isinstance(parsed_json.get("requests"), list)
        ) or any(
            isinstance(record.get("kind"), int) and "v" in record
            for record in sample_records
        ):
            provider = "copilot"
        elif any(
            (
                record.get("kind") == "main"
                and "sessionId" in record
                and "projectHash" in record
            )
            or (
                isinstance(record.get("$set"), Mapping)
                and "messages" in record.get("$set", {})
            )
            for record in sample_records
        ):
            provider = "gemini"

    if not provider:
        raise ExternalConversationImportError(
            "Von could not detect a supported Codex, Claude Code, Gemini, or "
            "VS Code/Copilot transcript.",
            error_code="external_conversation_format_not_detected",
        )
    return provider, parsed_json, source_format


def _parse_codex_records(
    records: Iterable[Mapping[str, Any]],
    *,
    invalid: int,
    source_sha256: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    session_id: str | None = None
    parent_conversation_id: str | None = None
    source_title: str | None = None
    events: list[ExternalConversationEvent] = []
    response_messages_seen = False
    fallback_records: list[Mapping[str, Any]] = []
    unsupported = invalid
    instruction_events = 0

    for record in records:
        envelope_type = _clean_text(record.get("type"), maximum=80)
        payload = record.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        locator = f"line:{record.get('__line_number')}"
        timestamp = _iso_timestamp(record.get("timestamp") or payload.get("timestamp"))
        if envelope_type == "session_meta":
            session_id = (
                _clean_text(payload.get("id"), maximum=240)
                or _clean_text(payload.get("session_id"), maximum=240)
                or session_id
            )
            parent_conversation_id = (
                _clean_text(payload.get("forked_from_id"), maximum=240)
                or _clean_text(payload.get("parent_thread_id"), maximum=240)
                or parent_conversation_id
            )
            source_workspace = source_workspace or _clean_text(
                payload.get("cwd"), maximum=500
            )
            continue

        payload_type = _clean_text(payload.get("type"), maximum=80)
        if envelope_type == "response_item" and payload_type == "message":
            role = _clean_text(payload.get("role"), maximum=40)
            if role not in {"user", "assistant"}:
                instruction_events += 1
                continue
            blocks = _text_blocks(payload.get("content"))
            if not blocks:
                unsupported += 1
                continue
            response_messages_seen = True
            event = ExternalConversationEvent(
                event_id=_stable_event_id(
                    provider="codex",
                    source_session_id=session_id or "unknown",
                    raw_locator=locator,
                    event_kind="message",
                    source_id=payload.get("id"),
                ),
                event_kind="message",
                source_role=role,
                actor=_actor_for_role("codex", role),
                content_blocks=blocks,
                source_timestamp_utc=timestamp,
                raw_locator=locator,
                model=_clean_text(payload.get("model"), maximum=160),
                user_visible=True,
            )
            events.append(event)
            if source_title is None and role == "user":
                source_title = event.visible_text[:80]
            continue

        if envelope_type == "event_msg" and payload_type in {
            "user_message",
            "agent_message",
        }:
            fallback_records.append(record)
            continue

        tool_like = bool(
            payload_type
            and any(token in payload_type for token in ("tool", "function_call"))
        )
        if envelope_type == "response_item" and tool_like:
            events.append(
                ExternalConversationEvent(
                    event_id=_stable_event_id(
                        provider="codex",
                        source_session_id=session_id or "unknown",
                        raw_locator=locator,
                        event_kind="tool_event",
                        source_id=payload.get("call_id") or payload.get("id"),
                    ),
                    event_kind="tool_event",
                    source_role="tool",
                    actor=_actor_for_role("codex", "tool"),
                    content_blocks=(),
                    source_timestamp_utc=timestamp,
                    raw_locator=locator,
                    user_visible=False,
                )
            )
        elif envelope_type not in {
            "event_msg",
            "response_item",
            "turn_context",
            "world_state",
        }:
            unsupported += 1

    if not response_messages_seen:
        for record in fallback_records:
            payload = record.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            payload_type = _clean_text(payload.get("type"), maximum=80)
            role = "user" if payload_type == "user_message" else "assistant"
            text_value = payload.get("message")
            blocks = _text_blocks(
                text_value if isinstance(text_value, list) else [text_value]
            )
            if not blocks:
                unsupported += 1
                continue
            locator = f"line:{record.get('__line_number')}"
            events.append(
                ExternalConversationEvent(
                    event_id=_stable_event_id(
                        provider="codex",
                        source_session_id=session_id or "unknown",
                        raw_locator=locator,
                        event_kind="message",
                    ),
                    event_kind="message",
                    source_role=role,
                    actor=_actor_for_role("codex", role),
                    content_blocks=blocks,
                    source_timestamp_utc=_iso_timestamp(record.get("timestamp")),
                    raw_locator=locator,
                    user_visible=True,
                )
            )

    session_id = session_id or f"codex-{source_sha256[:24]}"
    return _finish_package(
        provider="codex",
        source_session_id=session_id,
        source_title=source_title,
        source_account=source_account,
        source_workspace=source_workspace,
        source_format="codex_jsonl",
        source_sha256=source_sha256,
        parser_id="codex_jsonl.v1",
        events=events,
        parent_conversation_id=parent_conversation_id,
        loss_report={
            "invalid_or_unsupported_record_count": unsupported,
            "instruction_event_count": instruction_events,
            "branch_information_present": bool(parent_conversation_id),
        },
    )


def _parse_codex(
    text: str,
    *,
    source_sha256: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    records, invalid = _parse_json_lines(text)
    return _parse_codex_records(
        records,
        invalid=invalid,
        source_sha256=source_sha256,
        source_account=source_account,
        source_workspace=source_workspace,
    )


def _parse_claude_records(
    records: Iterable[Mapping[str, Any]],
    *,
    invalid: int,
    source_sha256: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    session_id: str | None = None
    source_title: str | None = None
    events: list[ExternalConversationEvent] = []
    unsupported = invalid
    branch_count = 0
    instruction_events = 0

    for record in records:
        record_type = _clean_text(record.get("type"), maximum=80)
        session_id = _clean_text(record.get("sessionId"), maximum=240) or session_id
        source_workspace = source_workspace or _clean_text(
            record.get("cwd"), maximum=500
        )
        if record_type in {"ai-title", "custom-title", "title"}:
            source_title = (
                _clean_text(record.get("title"), maximum=160)
                or _clean_text(record.get("content"), maximum=160)
                or source_title
            )
            continue
        if record_type not in {"user", "assistant", "system"}:
            unsupported += 1
            continue
        message = record.get("message")
        message = message if isinstance(message, Mapping) else {}
        role = _clean_text(message.get("role"), maximum=40) or record_type
        locator = f"line:{record.get('__line_number')}"
        parent_event_id = _clean_text(record.get("parentUuid"), maximum=240)
        branch_id = "sidechain" if record.get("isSidechain") is True else None
        if branch_id:
            branch_count += 1
        content = message.get("content")
        content_items = content if isinstance(content, list) else [content]
        visible_blocks = _text_blocks(content_items)
        if role in {"user", "assistant"} and visible_blocks:
            event = ExternalConversationEvent(
                event_id=_stable_event_id(
                    provider="claude_code",
                    source_session_id=session_id or "unknown",
                    raw_locator=locator,
                    event_kind="message",
                    source_id=record.get("uuid") or message.get("id"),
                ),
                event_kind="message",
                source_role=role,
                actor=_actor_for_role("claude_code", role),
                content_blocks=visible_blocks,
                source_timestamp_utc=_iso_timestamp(record.get("timestamp")),
                raw_locator=locator,
                parent_event_id=parent_event_id,
                branch_id=branch_id,
                model=_clean_text(message.get("model"), maximum=160),
                user_visible=True,
            )
            events.append(event)
            if source_title is None and role == "user":
                source_title = event.visible_text[:80]
        elif role == "system":
            instruction_events += 1

        for index, block in enumerate(content_items):
            if not isinstance(block, Mapping):
                continue
            block_type = _clean_text(block.get("type"), maximum=80)
            if block_type not in {
                "tool_use",
                "tool_result",
                "thinking",
                "redacted_thinking",
            }:
                continue
            event_kind = {
                "tool_use": "tool_call",
                "tool_result": "tool_result",
                "thinking": "reasoning",
                "redacted_thinking": "reasoning",
            }[block_type]
            actor_role = "tool" if block_type == "tool_result" else "assistant"
            events.append(
                ExternalConversationEvent(
                    event_id=_stable_event_id(
                        provider="claude_code",
                        source_session_id=session_id or "unknown",
                        raw_locator=f"{locator}:content:{index}",
                        event_kind=event_kind,
                        source_id=block.get("id"),
                    ),
                    event_kind=event_kind,
                    source_role=actor_role,
                    actor=_actor_for_role("claude_code", actor_role),
                    content_blocks=(),
                    source_timestamp_utc=_iso_timestamp(record.get("timestamp")),
                    raw_locator=f"{locator}:content:{index}",
                    parent_event_id=parent_event_id,
                    branch_id=branch_id,
                    model=_clean_text(message.get("model"), maximum=160),
                    user_visible=False,
                )
            )

    session_id = session_id or f"claude-{source_sha256[:24]}"
    return _finish_package(
        provider="claude_code",
        source_session_id=session_id,
        source_title=source_title,
        source_account=source_account,
        source_workspace=source_workspace,
        source_format="claude_code_jsonl",
        source_sha256=source_sha256,
        parser_id="claude_code_jsonl.v1",
        events=events,
        loss_report={
            "invalid_or_unsupported_record_count": unsupported,
            "instruction_event_count": instruction_events,
            "sidechain_record_count": branch_count,
            "branch_information_present": branch_count > 0,
        },
    )


def _parse_claude(
    text: str,
    *,
    source_sha256: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    records, invalid = _parse_json_lines(text)
    return _parse_claude_records(
        records,
        invalid=invalid,
        source_sha256=source_sha256,
        source_account=source_account,
        source_workspace=source_workspace,
    )


def _parse_gemini_records(
    records: Iterable[Mapping[str, Any]],
    *,
    invalid: int,
    source_sha256: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    state: dict[str, Any] = {}
    journal_records = 0
    unsupported = invalid
    for record in records:
        if isinstance(record.get("$set"), Mapping):
            journal_records += 1
            for raw_path, value in record["$set"].items():
                path = [part for part in str(raw_path).split(".") if part]
                state = _set_nested_value(state, path, value, append=False)
            continue
        if isinstance(record.get("$push"), Mapping):
            journal_records += 1
            for raw_path, value in record["$push"].items():
                path = [part for part in str(raw_path).split(".") if part]
                state = _set_nested_value(state, path, value, append=True)
            continue
        for key, value in record.items():
            if key != "__line_number":
                state[key] = copy.deepcopy(value)

    session_id = _clean_text(state.get("sessionId"), maximum=240) or (
        f"gemini-{source_sha256[:24]}"
    )
    messages = state.get("messages")
    messages = messages if isinstance(messages, list) else []
    events: list[ExternalConversationEvent] = []
    assistant_count = 0
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            unsupported += 1
            continue
        source_type = (_clean_text(message.get("type"), maximum=80) or "").lower()
        role = {
            "user": "user",
            "human": "user",
            "assistant": "assistant",
            "model": "assistant",
            "gemini": "assistant",
        }.get(source_type)
        if role is None:
            unsupported += 1
            continue
        blocks = _text_blocks(message.get("content"))
        if not blocks:
            unsupported += 1
            continue
        if role == "assistant":
            assistant_count += 1
        locator = f"messages:{index}"
        events.append(
            ExternalConversationEvent(
                event_id=_stable_event_id(
                    provider="gemini",
                    source_session_id=session_id,
                    raw_locator=locator,
                    event_kind="message",
                    source_id=message.get("id"),
                ),
                event_kind="message",
                source_role=role,
                actor=_actor_for_role("gemini", role),
                content_blocks=blocks,
                source_timestamp_utc=_iso_timestamp(message.get("timestamp")),
                raw_locator=locator,
                model=_clean_text(message.get("model"), maximum=160),
                user_visible=True,
            )
        )
    source_workspace = source_workspace or _clean_text(
        state.get("projectHash"), maximum=500
    )
    return _finish_package(
        provider="gemini",
        source_session_id=session_id,
        source_title=_clean_text(state.get("title"), maximum=160),
        source_account=source_account,
        source_workspace=source_workspace,
        source_format="gemini_chat_journal_jsonl",
        source_sha256=source_sha256,
        parser_id="gemini_chat_journal.v1",
        events=events,
        loss_report={
            "invalid_or_unsupported_record_count": unsupported,
            "journal_record_count": journal_records,
            "assistant_message_count": assistant_count,
            "source_has_no_assistant_messages": assistant_count == 0,
            "branch_information_present": False,
        },
    )


def _parse_gemini(
    text: str,
    *,
    source_sha256: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    records, invalid = _parse_json_lines(text)
    return _parse_gemini_records(
        records,
        invalid=invalid,
        source_sha256=source_sha256,
        source_account=source_account,
        source_workspace=source_workspace,
    )


def _set_nested_value(
    root: Any, path: Sequence[Any], value: Any, *, append: bool
) -> Any:
    if not path:
        if append:
            current = root if isinstance(root, list) else []
            additions = value if isinstance(value, list) else [value]
            return [*current, *copy.deepcopy(additions)]
        return copy.deepcopy(value)
    if len(path) > 64:
        raise ExternalConversationImportError(
            "The VS Code transcript contains an unsafe journal path.",
            error_code="external_conversation_journal_path_invalid",
        )
    current = root
    for index, key in enumerate(path[:-1]):
        next_key = path[index + 1]
        if isinstance(key, int):
            if not isinstance(current, list) or key < 0:
                raise ExternalConversationImportError(
                    "The VS Code transcript contains an invalid array path.",
                    error_code="external_conversation_journal_path_invalid",
                )
            while len(current) <= key:
                current.append([] if isinstance(next_key, int) else {})
            if current[key] is None:
                current[key] = [] if isinstance(next_key, int) else {}
            current = current[key]
        else:
            if not isinstance(current, dict):
                raise ExternalConversationImportError(
                    "The VS Code transcript contains an invalid object path.",
                    error_code="external_conversation_journal_path_invalid",
                )
            if key not in current or current[key] is None:
                current[key] = [] if isinstance(next_key, int) else {}
            current = current[key]
    final = path[-1]
    if isinstance(final, int):
        if not isinstance(current, list) or final < 0:
            raise ExternalConversationImportError(
                "The VS Code transcript contains an invalid final array path.",
                error_code="external_conversation_journal_path_invalid",
            )
        while len(current) <= final:
            current.append(None)
        if append:
            existing = current[final] if isinstance(current[final], list) else []
            additions = value if isinstance(value, list) else [value]
            current[final] = [*existing, *copy.deepcopy(additions)]
        else:
            current[final] = copy.deepcopy(value)
    else:
        if not isinstance(current, dict):
            raise ExternalConversationImportError(
                "The VS Code transcript contains an invalid final object path.",
                error_code="external_conversation_journal_path_invalid",
            )
        if append:
            existing = current.get(final)
            existing = existing if isinstance(existing, list) else []
            additions = value if isinstance(value, list) else [value]
            current[final] = [*existing, *copy.deepcopy(additions)]
        else:
            current[final] = copy.deepcopy(value)
    return root


def _replay_vscode_journal(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    state: Any = {}
    for record in records:
        kind = record.get("kind")
        if kind == 0:
            state = copy.deepcopy(record.get("v"))
            if not isinstance(state, dict):
                state = {}
            continue
        path = record.get("k")
        if kind not in {1, 2} or not isinstance(path, list):
            continue
        state = _set_nested_value(
            state,
            path,
            record.get("v"),
            append=kind == 2,
        )
    return state if isinstance(state, Mapping) else {}


def _nested_visible_text(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_nested_visible_text(item, depth=depth + 1))
        return output
    if not isinstance(value, Mapping):
        return []
    output: list[str] = []
    for key in ("text", "value", "content", "markdown"):
        if key in value:
            output.extend(_nested_visible_text(value.get(key), depth=depth + 1))
    return output


def _copilot_response_text_and_tools(
    response: Any,
) -> tuple[str, list[tuple[int, Mapping[str, Any]]]]:
    parts = response if isinstance(response, list) else [response]
    text_parts: list[str] = []
    tool_parts: list[tuple[int, Mapping[str, Any]]] = []
    for index, item in enumerate(parts):
        if isinstance(item, str):
            if item.strip():
                text_parts.append(item.strip())
            continue
        if not isinstance(item, Mapping):
            continue
        kind = (_clean_text(item.get("kind"), maximum=120) or "").lower()
        tool_like = bool(
            item.get("toolCallId")
            or item.get("toolId")
            or "tool" in kind
            or "progress" in kind
            or "invocation" in kind
        )
        if tool_like:
            tool_parts.append((index, item))
            continue
        candidates: list[str] = []
        for key in ("value", "content", "markdown"):
            if key in item:
                candidates.extend(_nested_visible_text(item.get(key)))
        for candidate in candidates:
            if candidate and candidate not in text_parts:
                text_parts.append(candidate)
    return "\n\n".join(text_parts).strip(), tool_parts


def _parse_copilot(
    text: str,
    *,
    parsed_json: Any,
    source_sha256: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    invalid = 0
    if isinstance(parsed_json, Mapping) and isinstance(
        parsed_json.get("requests"), list
    ):
        state = parsed_json
        source_format = "vscode_chat_export_json"
    else:
        records, invalid = _parse_json_lines(text)
        state = _replay_vscode_journal(records)
        source_format = "vscode_chat_session_journal_jsonl"

    session_id = (
        _clean_text(state.get("sessionId"), maximum=240)
        or f"copilot-{source_sha256[:24]}"
    )
    source_title = _clean_text(state.get("customTitle"), maximum=160) or _clean_text(
        state.get("title"), maximum=160
    )
    requests = state.get("requests")
    if not isinstance(requests, list):
        requests = []
    events: list[ExternalConversationEvent] = []
    unsupported = invalid
    hidden_count = 0

    for request_index, item in enumerate(requests):
        if not isinstance(item, Mapping):
            unsupported += 1
            continue
        locator = f"requests:{request_index}"
        hidden = item.get("hiddenFromTranscript") is True
        if hidden:
            hidden_count += 1
        message = item.get("message")
        message_text = None
        if isinstance(message, Mapping):
            message_text = _clean_text(message.get("text"))
            if not message_text:
                message_text = "\n\n".join(
                    _nested_visible_text(message.get("parts"))
                ).strip()
        elif isinstance(message, str):
            message_text = _clean_text(message)
        timestamp = _iso_timestamp(item.get("timestamp"))
        request_id = item.get("requestId") or item.get("id")
        if message_text:
            events.append(
                ExternalConversationEvent(
                    event_id=_stable_event_id(
                        provider="copilot",
                        source_session_id=session_id,
                        raw_locator=f"{locator}:message",
                        event_kind="message",
                        source_id=request_id,
                    ),
                    event_kind="message",
                    source_role="user",
                    actor=_actor_for_role("copilot", "user"),
                    content_blocks=(
                        ExternalContentBlock(
                            block_kind="text",
                            text=message_text,
                            source_type="vscode_request_message",
                        ),
                    ),
                    source_timestamp_utc=timestamp,
                    raw_locator=f"{locator}:message",
                    user_visible=not hidden,
                )
            )
            source_title = source_title or message_text[:80]

        response_text, tool_parts = _copilot_response_text_and_tools(
            item.get("response")
        )
        response_timestamp = _iso_timestamp(item.get("responseTimestamp")) or timestamp
        if response_text:
            events.append(
                ExternalConversationEvent(
                    event_id=_stable_event_id(
                        provider="copilot",
                        source_session_id=session_id,
                        raw_locator=f"{locator}:response",
                        event_kind="message",
                        source_id=item.get("responseId"),
                    ),
                    event_kind="message",
                    source_role="assistant",
                    actor=_actor_for_role("copilot", "assistant"),
                    content_blocks=(
                        ExternalContentBlock(
                            block_kind="text",
                            text=response_text,
                            source_type="vscode_response",
                        ),
                    ),
                    source_timestamp_utc=response_timestamp,
                    raw_locator=f"{locator}:response",
                    model=_clean_text(item.get("modelId"), maximum=160),
                    user_visible=not hidden,
                )
            )
        for part_index, tool_part in tool_parts:
            events.append(
                ExternalConversationEvent(
                    event_id=_stable_event_id(
                        provider="copilot",
                        source_session_id=session_id,
                        raw_locator=f"{locator}:response:{part_index}",
                        event_kind="tool_event",
                        source_id=tool_part.get("toolCallId"),
                    ),
                    event_kind="tool_event",
                    source_role="tool",
                    actor=_actor_for_role("copilot", "tool"),
                    content_blocks=(),
                    source_timestamp_utc=response_timestamp,
                    raw_locator=f"{locator}:response:{part_index}",
                    model=_clean_text(item.get("modelId"), maximum=160),
                    user_visible=False,
                )
            )

    return _finish_package(
        provider="copilot",
        source_session_id=session_id,
        source_title=source_title,
        source_account=source_account,
        source_workspace=source_workspace,
        source_format=source_format,
        source_sha256=source_sha256,
        parser_id="vscode_copilot_chat.v1",
        events=events,
        loss_report={
            "invalid_or_unsupported_record_count": unsupported,
            "hidden_request_count": hidden_count,
            "branch_information_present": False,
        },
    )


def _stream_jsonl_records(
    path: Path,
    *,
    statistics: dict[str, int],
) -> Iterable[Mapping[str, Any]]:
    """Yield bounded JSONL records without allowing one line to fill memory."""

    with path.open("rb") as handle:
        line_number = 0
        observed_records = 0
        while True:
            raw = handle.readline(MAX_EXTERNAL_CONVERSATION_JSONL_LINE_BYTES + 1)
            if not raw:
                break
            line_number += 1
            if len(raw) > MAX_EXTERNAL_CONVERSATION_JSONL_LINE_BYTES:
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(
                        MAX_EXTERNAL_CONVERSATION_JSONL_LINE_BYTES + 1
                    )
                statistics["oversized_record_count"] = (
                    statistics.get("oversized_record_count", 0) + 1
                )
                observed_records += 1
                if observed_records > MAX_STREAMED_EXTERNAL_CONVERSATION_RECORDS:
                    raise ExternalConversationImportError(
                        "The source contains too many records.",
                        error_code="external_conversation_record_limit_exceeded",
                    )
                continue
            if not raw.strip():
                continue
            observed_records += 1
            if observed_records > MAX_STREAMED_EXTERNAL_CONVERSATION_RECORDS:
                raise ExternalConversationImportError(
                    "The source contains too many records.",
                    error_code="external_conversation_record_limit_exceeded",
                )
            try:
                text = raw.decode("utf-8-sig" if line_number == 1 else "utf-8")
                value = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                statistics["invalid_record_count"] = (
                    statistics.get("invalid_record_count", 0) + 1
                )
                continue
            if not isinstance(value, Mapping):
                statistics["invalid_record_count"] = (
                    statistics.get("invalid_record_count", 0) + 1
                )
                continue
            record = dict(value)
            record["__line_number"] = line_number
            yield record


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_streamed_jsonl_file(
    path: Path,
    *,
    provider: str,
    source_account: str | None,
    source_workspace: str | None,
) -> ExternalConversationPackage:
    before = path.stat()
    source_sha256 = _sha256_file(path)
    statistics: dict[str, int] = {
        "invalid_record_count": 0,
        "oversized_record_count": 0,
    }
    records = _stream_jsonl_records(path, statistics=statistics)
    if provider == "codex":
        package = _parse_codex_records(
            records,
            invalid=0,
            source_sha256=source_sha256,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    elif provider == "claude_code":
        package = _parse_claude_records(
            records,
            invalid=0,
            source_sha256=source_sha256,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    elif provider == "gemini":
        package = _parse_gemini_records(
            records,
            invalid=0,
            source_sha256=source_sha256,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    else:
        raise ExternalConversationImportError(
            f"Large streamed {provider} files are not supported.",
            error_code="external_conversation_streaming_provider_unsupported",
        )
    package.loss_report["invalid_or_unsupported_record_count"] = (
        int(package.loss_report.get("invalid_or_unsupported_record_count", 0))
        + statistics["invalid_record_count"]
    )
    package.loss_report["oversized_record_count"] = statistics["oversized_record_count"]
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ExternalConversationImportError(
            "The source changed while it was being read; it can be retried safely.",
            error_code="external_conversation_source_changed_during_import",
        )
    return package


def parse_external_conversation_bytes(
    raw_bytes: bytes,
    *,
    provider_hint: str | None = None,
    source_account: str | None = None,
    source_workspace: str | None = None,
) -> ExternalConversationPackage:
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise ExternalConversationImportError(
            "The uploaded conversation file is empty.",
            error_code="external_conversation_source_empty",
        )
    if len(raw_bytes) > MAX_EXTERNAL_CONVERSATION_SOURCE_BYTES:
        raise ExternalConversationImportError(
            "The uploaded conversation exceeds the 32 MiB import limit.",
            error_code="external_conversation_source_too_large",
        )
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExternalConversationImportError(
            "The uploaded conversation is not valid UTF-8.",
            error_code="external_conversation_source_encoding_invalid",
        ) from exc
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    provider_hint = _normalise_provider(provider_hint)
    provider, parsed_json, _source_format = _detect_provider(text, provider_hint)
    if provider == "codex":
        return _parse_codex(
            text,
            source_sha256=source_sha256,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    if provider == "claude_code":
        return _parse_claude(
            text,
            source_sha256=source_sha256,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    if provider == "copilot":
        return _parse_copilot(
            text,
            parsed_json=parsed_json,
            source_sha256=source_sha256,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    if provider == "gemini":
        return _parse_gemini(
            text,
            source_sha256=source_sha256,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    raise ExternalConversationImportError(
        f"Unsupported provider: {provider}",
        error_code="unsupported_external_conversation_provider",
    )


def parse_external_conversation_file(
    path: Path,
    *,
    provider_hint: str | None = None,
    source_account: str | None = None,
    source_workspace: str | None = None,
) -> ExternalConversationPackage:
    source_path = path.expanduser().resolve()
    if not source_path.is_file():
        raise ExternalConversationImportError(
            "The external conversation source file does not exist.",
            error_code="external_conversation_source_missing",
        )
    provider = _normalise_provider(provider_hint)
    before = source_path.stat()
    if before.st_size > MAX_EXTERNAL_CONVERSATION_SOURCE_BYTES:
        if provider not in {"codex", "claude_code", "gemini"}:
            raise ExternalConversationImportError(
                "Large local transcripts require an explicit streamable provider.",
                error_code="external_conversation_source_too_large",
            )
        return _parse_streamed_jsonl_file(
            source_path,
            provider=provider,
            source_account=source_account,
            source_workspace=source_workspace,
        )
    raw_bytes = source_path.read_bytes()
    after = source_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ExternalConversationImportError(
            "The source changed while it was being read; it can be retried safely.",
            error_code="external_conversation_source_changed_during_import",
        )
    return parse_external_conversation_bytes(
        raw_bytes,
        provider_hint=provider,
        source_account=source_account,
        source_workspace=source_workspace,
    )


def _history_projection(
    package: ExternalConversationPackage,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    history: list[dict[str, Any]] = []
    total_chars = 0
    truncated_messages = 0
    truncated_characters = 0
    omitted_visible_messages = 0
    for event in package.events:
        if not event.user_visible or event.source_role not in {"user", "assistant"}:
            continue
        content = event.visible_text
        if not content:
            continue
        if (
            len(history) >= MAX_PROJECTED_MESSAGES
            or total_chars >= MAX_PROJECTED_TOTAL_CHARS
        ):
            omitted_visible_messages += 1
            continue
        remaining = MAX_PROJECTED_TOTAL_CHARS - total_chars
        maximum = min(MAX_PROJECTED_MESSAGE_CHARS, remaining)
        projected = content[:maximum]
        if len(projected) < len(content):
            truncated_messages += 1
            truncated_characters += len(content) - len(projected)
        timestamp = _iso_timestamp(event.source_timestamp_utc)
        history.append(
            {
                "role": event.source_role,
                "content": projected,
                "timestamp": timestamp or datetime.now(timezone.utc),
                "external_event_id": event.event_id,
                "external_event_kind": event.event_kind,
                "external_source_role": event.source_role,
                "external_actor": asdict(event.actor),
                "external_parent_event_id": event.parent_event_id,
                "external_branch_id": event.branch_id,
                "external_model": event.model,
                "external_raw_locator": event.raw_locator,
                "external_source_timestamp_utc": timestamp,
            }
        )
        total_chars += len(projected)
    projection_report = {
        "projected_message_count": len(history),
        "projected_character_count": total_chars,
        "truncated_message_count": truncated_messages,
        "truncated_character_count": truncated_characters,
        "omitted_visible_message_count": omitted_visible_messages,
        "projection_complete": not (
            truncated_messages or truncated_characters or omitted_visible_messages
        ),
    }
    return history, projection_report


class _SingleRecordAdapter:
    def __init__(self, record: SessionRecord) -> None:
        self.environment = record.environment
        self._record = record

    def discover(self) -> tuple[list[SessionRecord], list[str]]:
        return [self._record], []


def _build_source_record(
    *,
    path: Path,
    package: ExternalConversationPackage,
    custodian_user_id: str,
    namespace: str | None,
) -> SessionRecord:
    stat = path.stat()
    source_modified = (
        package.source_updated_at_utc
        or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    )
    custodian_scope = hashlib.sha256(
        f"{custodian_user_id}|{namespace or ''}".encode("utf-8")
    ).hexdigest()[:16]
    canonical_source_path = (
        f"external-import://custodian/{custodian_scope}/"
        f"{package.provider}/{package.source_session_id}"
    )
    return SessionRecord(
        environment=package.provider,
        source_session_id=package.source_session_id,
        canonical_source_path=canonical_source_path,
        source_uri=None,
        source_modified_at_utc=source_modified,
        source_size_bytes=int(stat.st_size),
        content_sha256=package.source_sha256,
        local_path=str(path.resolve()),
        title=package.source_title or path.name,
        metadata={
            "external_conversation_package_schema_version": (
                EXTERNAL_CONVERSATION_PACKAGE_SCHEMA_VERSION
            ),
            "external_conversation_parser_id": package.parser_id,
            "external_conversation_parser_version": package.parser_version,
        },
    )


def _preview_payload(
    *,
    package: ExternalConversationPackage,
    session_id: str,
    action: str,
    projection_report: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "external_conversation_import_preview.v1",
        "provider": package.provider,
        "source_session_id": package.source_session_id,
        "source_title": package.source_title,
        "source_format": package.source_format,
        "source_sha256": package.source_sha256,
        "package_sha256": package.package_sha256,
        "parser_id": package.parser_id,
        "parser_version": package.parser_version,
        "session_id": session_id,
        "action": action,
        "participant_count": len(package.participants),
        "participants": [asdict(actor) for actor in package.participants],
        "event_count": len(package.events),
        "loss_report": {
            **package.loss_report,
            **dict(projection_report),
        },
        "read_only": True,
        "private_by_default": True,
    }


def import_external_conversation_file(
    *,
    path: Path,
    custodian_user_id: str,
    namespace: str | None,
    organisation_concept_id: str | None,
    role_in_org: str | None,
    provider_hint: str | None = None,
    source_account: str | None = None,
    source_workspace: str | None = None,
    dry_run: bool = True,
    checkpoint_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    package = parse_external_conversation_file(
        path,
        provider_hint=provider_hint,
        source_account=source_account,
        source_workspace=source_workspace,
    )
    session_id = stable_imported_conversation_session_id(
        custodian_user_id=custodian_user_id,
        provider=package.provider,
        source_session_id=package.source_session_id,
        namespace=namespace,
        source_account=package.source_account,
        source_workspace=package.source_workspace,
    )
    history, projection_report = _history_projection(package)
    if not history:
        raise ExternalConversationImportError(
            "The source contains no user-visible human or assistant messages.",
            error_code="external_conversation_has_no_visible_messages",
        )

    source_identity_key = hashlib.sha256(
        "|".join(
            [
                custodian_user_id,
                namespace or "",
                package.provider,
                package.source_account or "",
                package.source_workspace or "",
                package.source_session_id,
            ]
        ).encode("utf-8")
    ).hexdigest()
    planned_action = chat_history_service.classify_external_conversation_projection(
        user_id=custodian_user_id,
        session_id=session_id,
        namespace=namespace,
        source_identity_key=source_identity_key,
        source_sha256=package.source_sha256,
        package_sha256=package.package_sha256,
        parser_version=package.parser_version,
    )
    preview = _preview_payload(
        package=package,
        session_id=session_id,
        action=planned_action,
        projection_report=projection_report,
    )
    if dry_run:
        return {
            "success": True,
            "status": "dry_run",
            "dry_run": True,
            "preview": preview,
            "canonical_read_back": None,
        }

    record = _build_source_record(
        path=path,
        package=package,
        custodian_user_id=custodian_user_id,
        namespace=namespace,
    )
    raw_result = AIChatSessionIngestionService(
        user_concept_id=custodian_user_id,
        adapters=[_SingleRecordAdapter(record)],
    ).run(dry_run=False)
    if not raw_result.success:
        return {
            "success": False,
            "status": "raw_source_ingestion_failed",
            "dry_run": False,
            "preview": preview,
            "raw_ingestion": raw_result.to_dict(),
            "error_code": raw_result.error_code or "raw_source_ingestion_failed",
        }

    raw_record_result = raw_result.records[0] if raw_result.records else {}
    raw_document_concept_id = record.document_concept_id
    if checkpoint_callback is not None:
        checkpoint_callback(
            "raw_ingested",
            {
                "completed": True,
                "source_sha256": package.source_sha256,
                "action": raw_record_result.get("action"),
                "document_concept_id": raw_document_concept_id,
                "file_copy_concept_id": raw_record_result.get("file_copy_concept_id"),
            },
        )
    imported_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": "external_conversation_import.v1",
        "package_schema_version": EXTERNAL_CONVERSATION_PACKAGE_SCHEMA_VERSION,
        "read_only": True,
        "trust_boundary": "imported_content_is_untrusted_historical_data",
        "custodian_user_id": custodian_user_id,
        "source_identity_key": source_identity_key,
        "provider": package.provider,
        "source_session_id": package.source_session_id,
        "source_account": package.source_account,
        "source_workspace": package.source_workspace,
        "source_title": package.source_title,
        "source_format": package.source_format,
        "source_sha256": package.source_sha256,
        "package_sha256": package.package_sha256,
        "parser_id": package.parser_id,
        "parser_version": package.parser_version,
        "source_created_at_utc": package.source_created_at_utc,
        "source_updated_at_utc": package.source_updated_at_utc,
        "parent_conversation_id": package.parent_conversation_id,
        "raw_document_concept_id": raw_document_concept_id,
        "raw_file_copy_concept_id": raw_record_result.get("file_copy_concept_id"),
        "participant_count": len(package.participants),
        "event_count": len(package.events),
        "participants": [asdict(actor) for actor in package.participants],
        "loss_report": {**package.loss_report, **projection_report},
        "imported_at_utc": imported_at,
    }
    projection = chat_history_service.upsert_external_conversation_projection(
        user_id=custodian_user_id,
        session_id=session_id,
        session_name=package.source_title or f"{package.provider} conversation",
        namespace=namespace,
        organisation_concept_id=organisation_concept_id,
        role_in_org=role_in_org,
        history=history,
        manifest=manifest,
    )
    if not projection.get("success"):
        return {
            "success": False,
            "status": "conversation_projection_failed",
            "dry_run": False,
            "preview": preview,
            "raw_ingestion": raw_result.to_dict(),
            "projection": projection,
            "error_code": projection.get("error_code")
            or "conversation_projection_failed",
        }
    if checkpoint_callback is not None:
        checkpoint_callback(
            "projection_reconciled",
            {
                "completed": True,
                "source_sha256": package.source_sha256,
                "package_sha256": package.package_sha256,
                "action": projection.get("action"),
                "session_id": session_id,
                "appended_message_count": projection.get("appended_message_count"),
                "divergence": projection.get("divergence"),
            },
        )

    raw_manifest_annotation_status = "unchanged"
    if (
        projection.get("action") != "unchanged"
        or raw_record_result.get("action") != "unchanged"
    ):
        try:
            concept_service.update_concept(
                raw_document_concept_id,
                {
                    "attributes.external_conversation_session_id": session_id,
                    "attributes.external_conversation_package_schema_version": (
                        EXTERNAL_CONVERSATION_PACKAGE_SCHEMA_VERSION
                    ),
                    "attributes.external_conversation_package_sha256": (
                        package.package_sha256
                    ),
                    "attributes.external_conversation_parser_id": package.parser_id,
                    "attributes.external_conversation_parser_version": (
                        package.parser_version
                    ),
                    "attributes.external_conversation_event_count": len(package.events),
                    "attributes.external_conversation_projection_message_count": len(
                        history
                    ),
                    "attributes.external_conversation_projection_complete": (
                        projection_report["projection_complete"]
                    ),
                },
            )
            raw_manifest_annotation_status = "updated"
        except Exception as exc:  # projection and raw source are already canonical
            raw_manifest_annotation_status = "pending"
            logger.warning(
                "External conversation raw-document annotation is pending for %s: %s",
                raw_document_concept_id,
                exc,
            )

    return {
        "success": True,
        "status": projection.get("action") or planned_action,
        "dry_run": False,
        "preview": preview,
        "raw_ingestion": {
            "status": raw_result.status,
            "action": raw_record_result.get("action"),
            "document_concept_id": raw_document_concept_id,
            "file_copy_concept_id": raw_record_result.get("file_copy_concept_id"),
        },
        "projection": {
            "action": projection.get("action"),
            "session_id": session_id,
            "appended_message_count": projection.get("appended_message_count"),
            "divergence": projection.get("divergence"),
        },
        "raw_manifest_annotation_status": raw_manifest_annotation_status,
        "canonical_read_back": projection.get("canonical_read_back"),
    }


def continue_external_conversation(
    *,
    custodian_user_id: str,
    source_session_id: str,
    namespace: str | None,
    organisation_concept_id: str | None,
    role_in_org: str | None,
) -> dict[str, Any]:
    source = chat_history_service.get_external_conversation_projection(
        user_id=custodian_user_id,
        session_id=source_session_id,
        namespace=namespace,
        include_history=True,
    )
    if not source:
        raise ExternalConversationImportError(
            "The imported conversation was not found for the current actor.",
            error_code="external_conversation_not_found",
        )
    manifest = source.get("external_conversation_import")
    if not isinstance(manifest, Mapping) or manifest.get("read_only") is not True:
        raise ExternalConversationImportError(
            "The selected conversation is not an imported read-only conversation.",
            error_code="external_conversation_not_read_only",
        )
    new_session_id = str(uuid.uuid4())
    source_name = (
        _clean_text(source.get("session_name"), maximum=80) or "Imported conversation"
    )
    continuation_name = f"{source_name} — continued"[:80]
    created = chat_history_service.create_chat_session(
        user_id=custodian_user_id,
        session_id=new_session_id,
        session_name=continuation_name,
        namespace=namespace,
        organisation_concept_id=organisation_concept_id,
        role_in_org=role_in_org,
        origin_kind="external_conversation_continuation",
    )
    history = source.get("history")
    history = copy.deepcopy(history) if isinstance(history, list) else []
    lineage = {
        "schema_version": EXTERNAL_CONVERSATION_LINEAGE_SCHEMA_VERSION,
        "lineage_kind": "native_continuation_of_external_snapshot",
        "forked_from_session_id": source_session_id,
        "source_provider": manifest.get("provider"),
        "source_session_id": manifest.get("source_session_id"),
        "source_snapshot_sha256": manifest.get("source_sha256"),
        "source_package_sha256": manifest.get("package_sha256"),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    update = chat_history_service.set_chat_history_for_session(
        user_id=custodian_user_id,
        session_id=new_session_id,
        history=history,
        namespace=namespace,
        set_updated_at=True,
        extra_set_fields={"conversation_lineage": lineage},
    )
    if not update.get("matched"):
        raise ExternalConversationImportError(
            "Von created the continuation but could not initialise its transcript.",
            error_code="external_conversation_continuation_initialisation_failed",
        )
    return {
        "success": True,
        "status": "created",
        "session_id": new_session_id,
        "session_name": created.get("session_name") or continuation_name,
        "history_message_count": len(history),
        "conversation_lineage": lineage,
    }


__all__ = [
    "EXTERNAL_CONVERSATION_PACKAGE_SCHEMA_VERSION",
    "EXTERNAL_CONVERSATION_PARSER_VERSION",
    "MAX_EXTERNAL_CONVERSATION_SOURCE_BYTES",
    "ExternalConversationImportError",
    "ExternalConversationPackage",
    "continue_external_conversation",
    "import_external_conversation_file",
    "parse_external_conversation_bytes",
    "parse_external_conversation_file",
    "stable_imported_conversation_session_id",
]
