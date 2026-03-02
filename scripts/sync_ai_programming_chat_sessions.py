from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from src.backend.services.ai_chat_session_ingestion_service import (  # noqa: E402
    AIChatSessionIngestionService,
    COPILOT_DEFAULT_ROOT,
    CodexSessionAdapter,
    CopilotSessionAdapter,
    GenericSessionAdapter,
)


logger = logging.getLogger(__name__)


def _resolve_user_concept_id(explicit_user_concept_id: str | None) -> str:
    if isinstance(explicit_user_concept_id, str) and explicit_user_concept_id.strip():
        return explicit_user_concept_id.strip()

    env_user = os.getenv("VON_USER_CONCEPT_ID")
    if isinstance(env_user, str) and env_user.strip():
        return env_user.strip()

    try:
        from src.backend.security.access_control import get_effective_user_concept_id

        inferred_user = get_effective_user_concept_id()
        if isinstance(inferred_user, str) and inferred_user.strip():
            return inferred_user.strip()
    except Exception:
        pass

    raise ValueError(
        "Unable to resolve user concept ID. Pass --user-concept-id or set VON_USER_CONCEPT_ID."
    )


def _discover_standard_copilot_roots() -> list[Path]:
    roots: list[Path] = [COPILOT_DEFAULT_ROOT]
    appdata = os.getenv("APPDATA")
    if not appdata:
        return roots

    user_roots = [
        Path(appdata) / "Code" / "User",
        Path(appdata) / "Code - Insiders" / "User",
    ]
    for user_root in user_roots:
        workspace_storage = user_root / "workspaceStorage"
        if workspace_storage.exists() and workspace_storage.is_dir():
            for workspace_dir in workspace_storage.iterdir():
                if not workspace_dir.is_dir():
                    continue
                chat_sessions = workspace_dir / "chatSessions"
                if chat_sessions.exists() and chat_sessions.is_dir():
                    roots.append(chat_sessions)

        empty_window_chat_sessions = user_root / "globalStorage" / "emptyWindowChatSessions"
        if empty_window_chat_sessions.exists() and empty_window_chat_sessions.is_dir():
            roots.append(empty_window_chat_sessions)

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _build_adapters(
    *,
    codex_root: str | None,
    copilot_roots: Sequence[str] | None,
    claude_roots: Sequence[str] | None,
    antigravity_roots: Sequence[str] | None,
) -> list:
    effective_codex_root = Path(codex_root).expanduser() if codex_root else None
    effective_copilot_roots = (
        [Path(root).expanduser() for root in copilot_roots]
        if copilot_roots
        else _discover_standard_copilot_roots()
    )
    effective_claude_roots = (
        [Path(root).expanduser() for root in claude_roots]
        if claude_roots
        else [
            Path.home() / ".claude" / "projects",
            Path.home() / ".claude" / "sessions",
            Path.home() / ".claude" / "conversations",
            Path.home() / ".claude" / "chats",
            Path.home() / ".claude" / "history",
        ]
    )
    effective_antigravity_roots = (
        [Path(root).expanduser() for root in antigravity_roots]
        if antigravity_roots
        else [
            Path.home() / ".antigravity" / "User" / "workspaceStorage",
            Path.home() / ".antigravity" / "User" / "globalStorage",
            Path.home() / ".antigravity" / "history",
            Path.home() / ".antigravity" / "chat",
        ]
    )

    return [
        CodexSessionAdapter(root=effective_codex_root),
        CopilotSessionAdapter(roots=effective_copilot_roots),
        GenericSessionAdapter(
            environment="claude_code",
            roots=effective_claude_roots,
            patterns=("*.jsonl", "*.json", "*.md", "*.txt"),
            name_tokens=("chat", "session", "conversation", "transcript"),
        ),
        GenericSessionAdapter(
            environment="antigravity",
            roots=effective_antigravity_roots,
            patterns=("*.jsonl", "*.json", "*.md", "*.txt"),
            name_tokens=("chat", "session", "conversation", "history"),
        ),
    ]


def _build_progress_callback(
    *,
    progress_jsonl_path: Path | None,
    emit_progress_ndjson: bool,
) -> Any:
    handle = None
    if progress_jsonl_path is not None:
        progress_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        handle = progress_jsonl_path.open("w", encoding="utf-8")

    def _callback(event: dict[str, Any]) -> None:
        event_json = json.dumps(event, ensure_ascii=True)
        logger.info("sync_incremental_progress=%s", event_json)
        if emit_progress_ndjson:
            print(event_json, file=sys.stderr, flush=True)
        if handle is not None:
            handle.write(event_json + "\n")
            handle.flush()

    _callback._handle = handle  # type: ignore[attr-defined]
    return _callback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronise historical AI-assisted programming chat-session files "
            "into Von as document/file-copy concepts with idempotent updates."
        )
    )
    parser.add_argument(
        "--user-concept-id",
        default=None,
        help="User concept ID for scoping created/updated concepts.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute writes. Default mode is dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on discovered records to process.",
    )
    parser.add_argument(
        "--codex-root",
        default=None,
        help="Optional Codex sessions root override.",
    )
    parser.add_argument(
        "--copilot-root",
        action="append",
        default=None,
        help=(
            "Copilot chat files root. Repeat to scan multiple roots. "
            r"Default includes W:\Microsoft Copilot Chat Files."
        ),
    )
    parser.add_argument(
        "--claude-root",
        action="append",
        default=None,
        help="Optional Claude Code sessions roots.",
    )
    parser.add_argument(
        "--antigravity-root",
        action="append",
        default=None,
        help="Optional Antigravity sessions roots.",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional file path to persist run summary JSON.",
    )
    parser.add_argument(
        "--progress-jsonl",
        default=None,
        help=(
            "Optional NDJSON file path for incremental per-record telemetry. "
            "If omitted and --output-json is set, defaults to <output-json>.progress.jsonl."
        ),
    )
    parser.add_argument(
        "--emit-progress-ndjson",
        action="store_true",
        help="Emit incremental telemetry events as NDJSON lines to stderr.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default: INFO).",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    progress_callback: Any | None = None
    try:
        user_concept_id = _resolve_user_concept_id(args.user_concept_id)
        adapters = _build_adapters(
            codex_root=args.codex_root,
            copilot_roots=args.copilot_root,
            claude_roots=args.claude_root,
            antigravity_roots=args.antigravity_root,
        )
        copilot_roots = [
            str(root)
            for adapter in adapters
            if isinstance(adapter, CopilotSessionAdapter)
            for root in adapter.roots
        ]
        logger.info("copilot_scan_roots=%s", json.dumps(copilot_roots, ensure_ascii=True))
        progress_jsonl_path = None
        if isinstance(args.progress_jsonl, str) and args.progress_jsonl.strip():
            progress_jsonl_path = Path(args.progress_jsonl.strip()).expanduser()
        elif isinstance(args.output_json, str) and args.output_json.strip():
            progress_jsonl_path = Path(
                args.output_json.strip() + ".progress.jsonl"
            ).expanduser()
        progress_callback = _build_progress_callback(
            progress_jsonl_path=progress_jsonl_path,
            emit_progress_ndjson=bool(args.emit_progress_ndjson),
        )
        service = AIChatSessionIngestionService(
            user_concept_id=user_concept_id,
            adapters=adapters,
        )
        result = service.run(
            dry_run=not bool(args.apply),
            limit=args.limit,
            progress_callback=progress_callback,
        )
        payload = result.to_dict()
        print(json.dumps(payload, indent=2, ensure_ascii=True))

        if isinstance(args.output_json, str) and args.output_json.strip():
            output_path = Path(args.output_json.strip()).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )

        if payload.get("status") in {"failed", "escalation_required"}:
            return 2
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        handle = None
        try:
            handle = getattr(progress_callback, "_handle", None)
        except Exception:
            handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
