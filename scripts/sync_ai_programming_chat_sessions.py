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
    CodexSessionAdapter,
    CopilotSessionAdapter,
    GeminiSessionAdapter,
    GenericSessionAdapter,
)
from src.backend.services.context_bundle_service import (  # noqa: E402
    create_or_update_context_bundle,
)
from src.backend.services.blob_store import (  # noqa: E402
    resolve_blob_store_backend_from_env,
)
from src.backend.utils.runtime_env import apply_repo_dotenv_overrides  # noqa: E402


logger = logging.getLogger(__name__)

DOTENV_OVERRIDE_KEYS = {
    "VON_USER_CONCEPT_ID",
    "VON_BLOB_STORE_BACKEND",
    "VON_BLOB_STORE_LOCAL_ROOT",
    "VON_S3_BUCKET",
    "VON_S3_PREFIX",
    "VON_S3_ENDPOINT_URL",
    "VON_S3_PUBLIC_BASE_URL",
    "VON_S3_REGION_NAME",
    "VON_S3_ADDRESSING_STYLE",
    "VON_SWIFT_S3_FAILOVER_ENABLE",
    "VON_S3_ACCESS_KEY_ID",
    "VON_S3_SECRET_ACCESS_KEY",
    "VON_S3_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "VON_SWIFT_CONTAINER",
    "VON_SWIFT_PREFIX",
    "VON_SWIFT_PUBLIC_BASE_URL",
    "OS_CLOUD",
    "OS_AUTH_URL",
    "OS_USERNAME",
    "OS_PASSWORD",
    "OS_PROJECT_NAME",
    "OS_USER_DOMAIN_NAME",
    "OS_PROJECT_DOMAIN_NAME",
    "OS_REGION_NAME",
}


def _apply_script_dotenv_overrides() -> None:
    applied = apply_repo_dotenv_overrides(DOTENV_OVERRIDE_KEYS)
    if applied:
        logger.info(
            "Applied repo .env overrides for %s key(s) for chat-session sync.",
            len(applied),
        )


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
    roots: list[Path] = []
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
    gemini_roots: Sequence[str] | None,
    claude_roots: Sequence[str] | None,
    antigravity_roots: Sequence[str] | None,
) -> list:
    effective_codex_root = Path(codex_root).expanduser() if codex_root else None
    effective_copilot_roots = (
        [Path(root).expanduser() for root in copilot_roots]
        if copilot_roots
        else _discover_standard_copilot_roots()
    )
    effective_gemini_roots = (
        [Path(root).expanduser() for root in gemini_roots]
        if gemini_roots
        else [Path.home() / ".gemini" / "antigravity" / "conversations"]
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
        GeminiSessionAdapter(roots=effective_gemini_roots),
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


def _default_backup_root() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    if isinstance(local_appdata, str) and local_appdata.strip():
        return (
            Path(local_appdata.strip())
            / "Von"
            / "backups"
            / "ai_chat_sessions"
        )
    return Path.home() / "AppData" / "Local" / "Von" / "backups" / "ai_chat_sessions"


def _materialise_context_bundle(
    *,
    bundle_name: str | None,
    bundle_description: str | None,
    user_concept_id: str,
) -> str | None:
    if not isinstance(bundle_name, str) or not bundle_name.strip():
        return None

    result = create_or_update_context_bundle(
        name=bundle_name.strip(),
        description=(
            bundle_description.strip()
            if isinstance(bundle_description, str) and bundle_description.strip()
            else None
        ),
        user_id=user_concept_id,
    )
    bundle_id = str(result.get("bundle_id") or "").strip()
    if not result.get("success") or not bundle_id:
        raise RuntimeError(f"Failed to create import context bundle: {result}")
    return bundle_id


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
            "Default scans standard VS Code/Insiders chat-session storage locations."
        ),
    )
    parser.add_argument(
        "--gemini-root",
        action="append",
        default=None,
        help=(
            "Gemini conversation root. Repeat to scan multiple roots. "
            "Default scans ~/.gemini/antigravity/conversations."
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
        "--backup-root",
        default=None,
        help=(
            "Optional on-disk backup root for copied session files. "
            "If omitted, apply mode defaults to a non-repo LOCALAPPDATA path."
        ),
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable on-disk backup copies for this run.",
    )
    parser.add_argument(
        "--context-bundle-id",
        action="append",
        default=None,
        help="Existing context bundle ID to attach to every imported session document.",
    )
    parser.add_argument(
        "--context-dossier-id",
        action="append",
        default=None,
        help="Existing context dossier ID to attach to every imported session document.",
    )
    parser.add_argument(
        "--create-context-bundle-name",
        default=None,
        help="Create one shared context bundle with this name and attach imported session documents to it.",
    )
    parser.add_argument(
        "--create-context-bundle-description",
        default=None,
        help="Optional description for the shared import context bundle.",
    )
    parser.add_argument(
        "--blob-backend",
        choices=("local", "swift", "s3"),
        default=None,
        help="Override VON_BLOB_STORE_BACKEND for this run without editing .env.",
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
        _apply_script_dotenv_overrides()
        if isinstance(args.blob_backend, str) and args.blob_backend.strip():
            os.environ["VON_BLOB_STORE_BACKEND"] = args.blob_backend.strip().lower()
        user_concept_id = _resolve_user_concept_id(args.user_concept_id)
        adapters = _build_adapters(
            codex_root=args.codex_root,
            copilot_roots=args.copilot_root,
            gemini_roots=args.gemini_root,
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
        gemini_roots = [
            str(root)
            for adapter in adapters
            if isinstance(adapter, GeminiSessionAdapter)
            for root in adapter.roots
        ]
        logger.info("gemini_scan_roots=%s", json.dumps(gemini_roots, ensure_ascii=True))
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
        context_bundle_ids = list(args.context_bundle_id or [])
        materialised_bundle_id = (
            _materialise_context_bundle(
                bundle_name=args.create_context_bundle_name,
                bundle_description=args.create_context_bundle_description,
                user_concept_id=user_concept_id,
            )
            if bool(args.apply)
            else None
        )
        if materialised_bundle_id:
            context_bundle_ids.append(materialised_bundle_id)

        backup_root = None
        if not bool(args.no_backup):
            backup_root = (
                Path(args.backup_root).expanduser()
                if isinstance(args.backup_root, str) and args.backup_root.strip()
                else _default_backup_root()
            )
        service = AIChatSessionIngestionService(
            user_concept_id=user_concept_id,
            adapters=adapters,
            context_bundle_ids=context_bundle_ids,
            context_dossier_ids=list(args.context_dossier_id or []),
            backup_root=backup_root,
        )
        result = service.run(
            dry_run=not bool(args.apply),
            limit=args.limit,
            progress_callback=progress_callback,
        )
        payload = result.to_dict()
        payload["scan_roots"] = {
            "copilot": copilot_roots,
            "gemini": gemini_roots,
        }
        payload["configured_backup_root"] = str(backup_root) if backup_root else None
        payload["configured_context_bundle_ids"] = context_bundle_ids
        payload["configured_context_dossier_ids"] = list(args.context_dossier_id or [])
        payload["blob_backend"] = resolve_blob_store_backend_from_env()
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
