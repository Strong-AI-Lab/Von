from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from . import concept_service
from .ai_chat_session_source_profile_vontology_service import (
    AIChatSessionSourceProfile,
    AIChatSessionSourceProfileAuthority,
    AIChatSessionSourceProfileAuthorityMissingError,
    ensure_canonical_ai_chat_session_source_profiles_from_seed_fixture,
    load_ai_chat_session_source_profile_authority,
)
from .computer_file_copy_service import import_local_file_copy
from .context_bundle_contracts import (
    HAS_CONTEXT_BUNDLE_PREDICATE_ID,
    HAS_CONTEXT_DOSSIER_PREDICATE_ID,
)
from .context_bundle_service import ensure_canonical_context_bundle_ontology
from .relationship_removal_service import remove_relationship
from .relationship_write_service import add_relationship
from ..security.visibility_predicates import CANONICAL_SPECIFIC_TO_USER_PREDICATE

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_path_uri(path: Path) -> str | None:
    try:
        return path.as_uri()
    except Exception:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _stable_session_digest(
    environment: str,
    source_session_id: str,
    canonical_source_path: str,
) -> str:
    payload = (
        f"{environment.strip().lower()}|"
        f"{source_session_id.strip()}|"
        f"{canonical_source_path.strip().lower()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_document_concept_id(
    *,
    environment: str,
    source_session_id: str,
    canonical_source_path: str,
) -> str:
    digest = _stable_session_digest(
        environment=environment,
        source_session_id=source_session_id,
        canonical_source_path=canonical_source_path,
    )
    return f"#V#ai_programming_chat_session_{environment}_{digest[:20]}"


@dataclass(frozen=True)
class SessionRecord:
    environment: str
    source_session_id: str
    canonical_source_path: str
    source_uri: str | None
    source_modified_at_utc: str
    source_size_bytes: int
    content_sha256: str
    local_path: str
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> tuple[str, str, str]:
        return (
            self.environment,
            self.source_session_id,
            self.canonical_source_path,
        )

    @property
    def document_concept_id(self) -> str:
        return stable_document_concept_id(
            environment=self.environment,
            source_session_id=self.source_session_id,
            canonical_source_path=self.canonical_source_path,
        )


@dataclass
class SessionDecision:
    action: str
    reason: str
    record: SessionRecord
    document_concept_id: str
    repair_file_copy_concept_id: str | None = None


@dataclass
class SyncCounters:
    discovered: int = 0
    classified_new: int = 0
    classified_updated: int = 0
    classified_unchanged: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    intended_mutations: int = 0
    executed_mutations: int = 0


class SessionSourceAdapter(Protocol):
    environment: str

    def discover(self) -> tuple[list[SessionRecord], list[str]]: ...


class _FileAdapterBase:
    environment: str
    roots: Sequence[Path]
    patterns: Sequence[str]
    required: bool

    def _iter_existing_roots(self) -> tuple[list[Path], list[str]]:
        warnings: list[str] = []
        existing: list[Path] = []
        for root in self.roots:
            expanded = root.expanduser()
            if expanded.exists() and expanded.is_dir():
                existing.append(expanded)
            elif self.required:
                warnings.append(
                    f"{self.environment}: required root missing: {expanded}"
                )
        return existing, warnings

    def _candidate_files(self) -> tuple[list[Path], list[str]]:
        roots, warnings = self._iter_existing_roots()
        files: list[Path] = []
        for root in roots:
            for pattern in self.patterns:
                try:
                    for path in root.rglob(pattern):
                        if path.is_file():
                            files.append(path)
                except Exception as exc:
                    warnings.append(
                        f"{self.environment}: scan failed for {root} ({exc})"
                    )
        files = sorted({path.resolve() for path in files})
        return files, warnings

    def _build_session_id(self, path: Path) -> str:
        surrogate = hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:16]
        return f"{self.environment}_{surrogate}"

    def _record_from_file(self, path: Path) -> SessionRecord:
        stat = path.stat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        canonical = str(path.resolve())
        return SessionRecord(
            environment=self.environment,
            source_session_id=self._build_session_id(path),
            canonical_source_path=canonical,
            source_uri=_safe_path_uri(path.resolve()),
            source_modified_at_utc=modified_at,
            source_size_bytes=int(stat.st_size),
            content_sha256=_sha256_file(path),
            local_path=canonical,
            title=path.stem,
            metadata={},
        )

    def discover(self) -> tuple[list[SessionRecord], list[str]]:
        files, warnings = self._candidate_files()
        records: list[SessionRecord] = []
        for path in files:
            try:
                records.append(self._record_from_file(path))
            except Exception as exc:
                warnings.append(f"{self.environment}: failed to read {path} ({exc})")
        return records, warnings


class CodexSessionAdapter(_FileAdapterBase):
    environment = "codex"
    patterns = ("*.jsonl",)
    required = False
    _session_file_id = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.IGNORECASE,
    )

    def __init__(self, root: Path | None = None) -> None:
        self.roots = (root,) if isinstance(root, Path) else ()

    def _build_session_id(self, path: Path) -> str:
        match = self._session_file_id.search(path.name)
        if match:
            return match.group(1).lower()
        return super()._build_session_id(path)


class CopilotSessionAdapter(_FileAdapterBase):
    environment = "copilot"
    patterns = ("*.md", "*.markdown", "*.txt", "*.json", "*.jsonl")
    required = True

    def __init__(self, roots: Sequence[Path] | None = None) -> None:
        self.roots = tuple(roots or ())

    def _record_from_file(self, path: Path) -> SessionRecord:
        if path.name.endswith("~"):
            raise ValueError("temporary backup file ignored")
        rec = super()._record_from_file(path)
        session_stem = path.stem.strip()
        session_id = (
            session_stem
            if session_stem
            else hashlib.sha1(rec.canonical_source_path.encode("utf-8")).hexdigest()[
                :16
            ]
        )
        return SessionRecord(
            environment=rec.environment,
            source_session_id=session_id,
            canonical_source_path=rec.canonical_source_path,
            source_uri=rec.source_uri,
            source_modified_at_utc=rec.source_modified_at_utc,
            source_size_bytes=rec.source_size_bytes,
            content_sha256=rec.content_sha256,
            local_path=rec.local_path,
            title=path.name,
            metadata={"extension": path.suffix.lower()},
        )


class GeminiSessionAdapter(_FileAdapterBase):
    environment = "gemini"
    patterns = ("*.pb", "*.json", "*.jsonl", "*.md", "*.txt")
    required = False
    _session_file_id = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.IGNORECASE,
    )

    def __init__(self, roots: Sequence[Path] | None = None) -> None:
        self.roots = tuple(roots or ())

    def _build_session_id(self, path: Path) -> str:
        match = self._session_file_id.search(path.name)
        if match:
            return match.group(1).lower()
        return super()._build_session_id(path)

    def _record_from_file(self, path: Path) -> SessionRecord:
        record = super()._record_from_file(path)
        return SessionRecord(
            environment=record.environment,
            source_session_id=record.source_session_id,
            canonical_source_path=record.canonical_source_path,
            source_uri=record.source_uri,
            source_modified_at_utc=record.source_modified_at_utc,
            source_size_bytes=record.source_size_bytes,
            content_sha256=record.content_sha256,
            local_path=record.local_path,
            title=path.name,
            metadata={"extension": path.suffix.lower()},
        )


class GenericSessionAdapter(_FileAdapterBase):
    def __init__(
        self,
        *,
        environment: str,
        roots: Sequence[Path],
        patterns: Sequence[str],
        name_tokens: Sequence[str],
        required_roots: bool = False,
    ) -> None:
        self.environment = environment
        self.roots = tuple(roots)
        self.patterns = tuple(patterns)
        self._name_tokens = tuple(token.lower() for token in name_tokens)
        self.required = bool(required_roots)

    def _is_session_like(self, path: Path) -> bool:
        lower_path = str(path).lower()
        return any(token in lower_path for token in self._name_tokens)

    def discover(self) -> tuple[list[SessionRecord], list[str]]:
        files, warnings = self._candidate_files()
        filtered = [path for path in files if self._is_session_like(path)]
        records: list[SessionRecord] = []
        for path in filtered:
            try:
                records.append(self._record_from_file(path))
            except Exception as exc:
                warnings.append(f"{self.environment}: failed to read {path} ({exc})")
        return records, warnings


@dataclass
class SyncResult:
    success: bool
    status: str
    dry_run: bool
    started_at_utc: str
    finished_at_utc: str
    user_concept_id: str
    counters: SyncCounters
    warnings: list[str]
    records: list[dict[str, Any]]
    error_code: str | None = None
    requires_follow_up: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "dry_run": self.dry_run,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "user_concept_id": self.user_concept_id,
            "counters": {
                "discovered": self.counters.discovered,
                "classified_new": self.counters.classified_new,
                "classified_updated": self.counters.classified_updated,
                "classified_unchanged": self.counters.classified_unchanged,
                "created": self.counters.created,
                "updated": self.counters.updated,
                "skipped": self.counters.skipped,
                "failed": self.counters.failed,
                "intended_mutations": self.counters.intended_mutations,
                "executed_mutations": self.counters.executed_mutations,
            },
            "warnings": list(self.warnings),
            "records": list(self.records),
            "error_code": self.error_code,
            "requires_follow_up": self.requires_follow_up,
        }


class AIChatSessionIngestionService:
    """Idempotent ingestion pipeline for AI-assisted programming chat sessions."""

    def __init__(
        self,
        *,
        user_concept_id: str,
        adapters: Sequence[SessionSourceAdapter] | None = None,
        context_bundle_ids: Sequence[str] = (),
        context_dossier_ids: Sequence[str] = (),
        backup_root: Path | None = None,
    ) -> None:
        if not isinstance(user_concept_id, str) or not user_concept_id.strip():
            raise ValueError("user_concept_id is required")
        self.user_concept_id = user_concept_id.strip()
        self._uses_default_adapters = adapters is None
        self.adapters = list(adapters) if adapters is not None else []
        self._default_adapter_warnings: list[str] = []
        self._source_profile_authority: AIChatSessionSourceProfileAuthority | None = (
            None
        )
        self.context_bundle_ids = self._normalise_concept_ids(context_bundle_ids)
        self.context_dossier_ids = self._normalise_concept_ids(context_dossier_ids)
        self.backup_root = (
            backup_root.expanduser().resolve()
            if isinstance(backup_root, Path)
            else None
        )

    def _load_source_profile_authority(self) -> AIChatSessionSourceProfileAuthority:
        if self._source_profile_authority is not None:
            return self._source_profile_authority
        authority, _diagnostics = load_ai_chat_session_source_profile_authority()
        self._source_profile_authority = authority
        return authority

    @staticmethod
    def _paths_from_templates(templates: Sequence[str]) -> tuple[Path, ...]:
        paths: list[Path] = []
        seen: set[str] = set()
        for template in templates:
            if not isinstance(template, str) or not template.strip():
                continue
            path = Path(template.strip()).expanduser()
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
        return tuple(paths)

    @staticmethod
    def _adapters_for_profile(
        profile: AIChatSessionSourceProfile,
    ) -> tuple[list[SessionSourceAdapter], list[str]]:
        roots = AIChatSessionIngestionService._paths_from_templates(
            profile.default_root_templates
        )
        adapter_kind = profile.adapter_kind.strip().lower()
        if adapter_kind == "codex_sessions":
            return [CodexSessionAdapter(root=root) for root in roots], []
        if adapter_kind == "copilot_files":
            return [CopilotSessionAdapter(roots=roots)], []
        if adapter_kind == "gemini_files":
            return [GeminiSessionAdapter(roots=roots)], []
        if adapter_kind == "generic_file_glob":
            return [
                GenericSessionAdapter(
                    environment=profile.environment,
                    roots=roots,
                    patterns=profile.file_patterns,
                    name_tokens=profile.name_tokens,
                    required_roots=profile.required_roots,
                )
            ], []
        return [], [
            f"{profile.environment}: unsupported represented adapter kind: {profile.adapter_kind}"
        ]

    def _ensure_default_adapters(self) -> None:
        if not self._uses_default_adapters or self.adapters:
            return
        try:
            authority = self._load_source_profile_authority()
        except AIChatSessionSourceProfileAuthorityMissingError as exc:
            self._default_adapter_warnings.append(
                f"source_profile_authority_missing:{exc}"
            )
            return
        adapters: list[SessionSourceAdapter] = []
        warnings: list[str] = []
        for profile in authority.profiles:
            profile_adapters, profile_warnings = self._adapters_for_profile(profile)
            adapters.extend(profile_adapters)
            warnings.extend(profile_warnings)
        self.adapters = adapters
        self._default_adapter_warnings.extend(warnings)

    @staticmethod
    def _normalise_concept_ids(values: Sequence[str]) -> tuple[str, ...]:
        output: list[str] = []
        seen: set[str] = set()
        for raw in values:
            if not isinstance(raw, str):
                continue
            cleaned = raw.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            output.append(cleaned)
        return tuple(output)

    def ensure_ontology_types(self) -> list[str]:
        warnings: list[str] = []
        try:
            report = (
                ensure_canonical_ai_chat_session_source_profiles_from_seed_fixture()
            )
            if not report.get("success"):
                warnings.append(
                    "source_profile_authority_bootstrap_failed:"
                    + json.dumps(report, ensure_ascii=True, sort_keys=True, default=str)
                )
            authority, _diagnostics = load_ai_chat_session_source_profile_authority()
            self._source_profile_authority = authority
        except AIChatSessionSourceProfileAuthorityMissingError as exc:
            warnings.append(
                "source_profile_authority_missing:"
                + json.dumps(
                    exc.diagnostics,
                    ensure_ascii=True,
                    sort_keys=True,
                    default=str,
                )
            )
        except Exception as exc:
            warnings.append(f"source_profile_authority_bootstrap_failed:{exc}")
        return warnings

    def validate_ontology_types(self) -> list[str]:
        try:
            authority, _diagnostics = load_ai_chat_session_source_profile_authority()
            self._source_profile_authority = authority
            return []
        except AIChatSessionSourceProfileAuthorityMissingError as exc:
            return [
                "source_profile_authority_missing:"
                + json.dumps(
                    exc.diagnostics,
                    ensure_ascii=True,
                    sort_keys=True,
                    default=str,
                )
            ]
        except Exception as exc:
            return [f"source_profile_authority_validation_failed:{exc}"]

    def _planned_backup_path(self, record: SessionRecord) -> Path | None:
        if self.backup_root is None:
            return None
        filename = Path(record.local_path).name or "session.bin"
        safe_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
        if not safe_filename:
            safe_filename = "session.bin"
        return (
            self.backup_root
            / record.environment
            / record.source_session_id
            / record.content_sha256
            / safe_filename
        )

    def _backup_record_file(self, record: SessionRecord) -> dict[str, Any]:
        backup_path = self._planned_backup_path(record)
        if backup_path is None:
            return {
                "backup_attempted": False,
                "backup_written": False,
                "backup_reused_existing": False,
                "backup_path": None,
                "backup_metadata_path": None,
            }

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = backup_path.with_suffix(backup_path.suffix + ".metadata.json")
        source_path = Path(record.local_path)
        reused_existing = False
        written = False
        if backup_path.exists():
            try:
                existing_hash = _sha256_file(backup_path)
            except Exception:
                existing_hash = None
            if existing_hash == record.content_sha256:
                reused_existing = True
            else:
                shutil.copy2(source_path, backup_path)
                written = True
        else:
            shutil.copy2(source_path, backup_path)
            written = True

        metadata_payload = {
            "schema_version": "ai_chat_session_backup.v1",
            "environment": record.environment,
            "source_session_id": record.source_session_id,
            "source_path": record.canonical_source_path,
            "source_uri": record.source_uri,
            "source_modified_at_utc": record.source_modified_at_utc,
            "source_size_bytes": record.source_size_bytes,
            "content_sha256": record.content_sha256,
            "document_concept_id": record.document_concept_id,
            "title": record.title,
            "backed_up_at_utc": _utc_now_iso(),
        }
        metadata_path.write_text(
            json.dumps(metadata_payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        return {
            "backup_attempted": True,
            "backup_written": written,
            "backup_reused_existing": reused_existing,
            "backup_path": str(backup_path),
            "backup_metadata_path": str(metadata_path),
        }

    @staticmethod
    def _relationship_targets(concept_doc: dict[str, Any], predicate: str) -> list[str]:
        relationships = concept_doc.get("relationships")
        if not isinstance(relationships, dict):
            return []
        raw = relationships.get(predicate)
        if raw is None and predicate.startswith("#V#"):
            raw = relationships.get(predicate[3:])
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str) and item.strip()]
        return []

    def _context_links_aligned(self, concept_doc: dict[str, Any]) -> bool:
        if self.context_bundle_ids:
            linked_bundle_ids = set(
                self._relationship_targets(concept_doc, HAS_CONTEXT_BUNDLE_PREDICATE_ID)
            )
            if any(
                bundle_id not in linked_bundle_ids
                for bundle_id in self.context_bundle_ids
            ):
                return False
        if self.context_dossier_ids:
            linked_dossier_ids = set(
                self._relationship_targets(
                    concept_doc, HAS_CONTEXT_DOSSIER_PREDICATE_ID
                )
            )
            if any(
                dossier_id not in linked_dossier_ids
                for dossier_id in self.context_dossier_ids
            ):
                return False
        return True

    def _ensure_requested_context_links(
        self, document_concept_id: str
    ) -> dict[str, Any]:
        if self.context_bundle_ids or self.context_dossier_ids:
            ensure_canonical_context_bundle_ontology(
                concept_ids=(
                    [HAS_CONTEXT_BUNDLE_PREDICATE_ID] if self.context_bundle_ids else []
                )
                + (
                    [HAS_CONTEXT_DOSSIER_PREDICATE_ID]
                    if self.context_dossier_ids
                    else []
                ),
                create_missing_concepts=True,
            )

        for bundle_id in self.context_bundle_ids:
            result = add_relationship(
                document_concept_id,
                HAS_CONTEXT_BUNDLE_PREDICATE_ID,
                bundle_id,
            )
            if not result.get("success"):
                raise RuntimeError(
                    f"failed_attach_context_bundle:{document_concept_id}:{bundle_id}:{result}"
                )

        for dossier_id in self.context_dossier_ids:
            result = add_relationship(
                document_concept_id,
                HAS_CONTEXT_DOSSIER_PREDICATE_ID,
                dossier_id,
            )
            if not result.get("success"):
                raise RuntimeError(
                    f"failed_attach_context_dossier:{document_concept_id}:{dossier_id}:{result}"
                )

        refreshed = self._get_concept(document_concept_id)
        return {
            "attached_context_bundle_ids": list(self.context_bundle_ids),
            "attached_context_dossier_ids": list(self.context_dossier_ids),
            "context_links_aligned": bool(
                isinstance(refreshed, dict) and self._context_links_aligned(refreshed)
            ),
        }

    def run(
        self,
        *,
        dry_run: bool = True,
        limit: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> SyncResult:
        started_at = _utc_now_iso()
        counters = SyncCounters()
        warnings = (
            self.validate_ontology_types() if dry_run else self.ensure_ontology_types()
        )
        decisions: list[SessionDecision] = []
        record_results: list[dict[str, Any]] = []

        if self._source_profile_authority_blocked(warnings):
            finished_at = _utc_now_iso()
            self._emit_progress(
                progress_callback,
                {
                    "event": "sync_completed",
                    "at_utc": finished_at,
                    "dry_run": dry_run,
                    "status": "escalation_required",
                    "success": False,
                    "error_code": "source_profile_authority_missing",
                    "requires_follow_up": True,
                    "counters": self._counters_to_dict(counters),
                },
            )
            return SyncResult(
                success=False,
                status="escalation_required",
                dry_run=dry_run,
                started_at_utc=started_at,
                finished_at_utc=finished_at,
                user_concept_id=self.user_concept_id,
                counters=counters,
                warnings=warnings,
                records=[],
                error_code="source_profile_authority_missing",
                requires_follow_up=True,
            )

        records, discovery_warnings = self.discover_records()
        warnings.extend(discovery_warnings)

        if isinstance(limit, int) and limit > 0:
            records = records[:limit]

        counters.discovered = len(records)
        self._emit_progress(
            progress_callback,
            {
                "event": "discovery_complete",
                "at_utc": _utc_now_iso(),
                "dry_run": dry_run,
                "discovered": counters.discovered,
                "warnings_count": len(warnings),
            },
        )

        for idx, record in enumerate(records, start=1):
            decision = self._classify_record(record)
            decisions.append(decision)
            if decision.action == "new":
                counters.classified_new += 1
                counters.intended_mutations += 1
            elif decision.action in {"updated", "repair"}:
                counters.classified_updated += 1
                counters.intended_mutations += 1
            elif decision.action == "unchanged":
                counters.classified_unchanged += 1
            self._emit_progress(
                progress_callback,
                {
                    "event": "record_classified",
                    "at_utc": _utc_now_iso(),
                    "dry_run": dry_run,
                    "index": idx,
                    "total": counters.discovered,
                    "environment": decision.record.environment,
                    "source_session_id": decision.record.source_session_id,
                    "document_concept_id": decision.document_concept_id,
                    "action": decision.action,
                    "reason": decision.reason,
                    "requires_mutation": decision.action != "unchanged",
                    "counters": self._counters_to_dict(counters),
                },
            )

        for idx, decision in enumerate(decisions, start=1):
            if decision.action == "unchanged":
                backup_info = (
                    self._backup_record_file(decision.record) if not dry_run else {}
                )
                counters.skipped += 1
                result_entry = {
                    "environment": decision.record.environment,
                    "source_session_id": decision.record.source_session_id,
                    "document_concept_id": decision.document_concept_id,
                    "action": "unchanged",
                    "reason": decision.reason,
                    "success": True,
                    "storage_object_written": False,
                    "ontology_links_aligned": True,
                    "ontology_type_aligned": True,
                    "context_links_aligned": True,
                    "attached_context_bundle_ids": list(self.context_bundle_ids),
                    "attached_context_dossier_ids": list(self.context_dossier_ids),
                    "planned_backup_path": (
                        str(self._planned_backup_path(decision.record))
                        if dry_run and self._planned_backup_path(decision.record)
                        else None
                    ),
                }
                result_entry.update(backup_info)
                record_results.append(result_entry)
                self._emit_record_processed_progress(
                    progress_callback=progress_callback,
                    dry_run=dry_run,
                    index=idx,
                    total=len(decisions),
                    decision=decision,
                    result_entry=result_entry,
                    counters=counters,
                )
                continue

            if dry_run:
                result_entry = {
                    "environment": decision.record.environment,
                    "source_session_id": decision.record.source_session_id,
                    "document_concept_id": decision.document_concept_id,
                    "action": f"would_{decision.action}",
                    "reason": decision.reason,
                    "success": True,
                    "storage_object_written": False,
                    "ontology_links_aligned": None,
                    "ontology_type_aligned": None,
                    "context_links_aligned": None,
                    "attached_context_bundle_ids": list(self.context_bundle_ids),
                    "attached_context_dossier_ids": list(self.context_dossier_ids),
                    "planned_backup_path": (
                        str(self._planned_backup_path(decision.record))
                        if self._planned_backup_path(decision.record)
                        else None
                    ),
                }
                record_results.append(result_entry)
                self._emit_record_processed_progress(
                    progress_callback=progress_callback,
                    dry_run=dry_run,
                    index=idx,
                    total=len(decisions),
                    decision=decision,
                    result_entry=result_entry,
                    counters=counters,
                )
                continue

            backup_info = self._backup_record_file(decision.record)

            if decision.action == "new":
                applied = self._apply_new(decision.record, decision.document_concept_id)
            elif decision.action == "repair":
                applied = self._apply_repair(
                    decision.record,
                    decision.document_concept_id,
                    decision.repair_file_copy_concept_id,
                )
            else:
                applied = self._apply_update(
                    decision.record, decision.document_concept_id
                )

            if applied.get("success"):
                counters.executed_mutations += 1
                if decision.action == "new":
                    counters.created += 1
                else:
                    counters.updated += 1
            else:
                counters.failed += 1

            result_entry = {
                "environment": decision.record.environment,
                "source_session_id": decision.record.source_session_id,
                "document_concept_id": decision.document_concept_id,
                "action": decision.action,
                "reason": decision.reason,
            }
            result_entry.update(applied)
            result_entry.update(backup_info)
            record_results.append(result_entry)
            self._emit_record_processed_progress(
                progress_callback=progress_callback,
                dry_run=dry_run,
                index=idx,
                total=len(decisions),
                decision=decision,
                result_entry=result_entry,
                counters=counters,
            )

        status = "dry_run" if dry_run else "ok"
        error_code: str | None = None
        requires_follow_up = False

        if not dry_run:
            if counters.failed > 0:
                status = "failed"
            if counters.intended_mutations > 0 and counters.executed_mutations == 0:
                status = "escalation_required"
                error_code = "mutation_not_executed"
                requires_follow_up = True

        finished_at = _utc_now_iso()
        success = status in {"ok", "dry_run"}
        self._emit_progress(
            progress_callback,
            {
                "event": "sync_completed",
                "at_utc": finished_at,
                "dry_run": dry_run,
                "status": status,
                "success": success,
                "error_code": error_code,
                "requires_follow_up": requires_follow_up,
                "counters": self._counters_to_dict(counters),
            },
        )

        return SyncResult(
            success=success,
            status=status,
            dry_run=dry_run,
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            user_concept_id=self.user_concept_id,
            counters=counters,
            warnings=warnings,
            records=record_results,
            error_code=error_code,
            requires_follow_up=requires_follow_up,
        )

    @staticmethod
    def _emit_progress(
        progress_callback: ProgressCallback | None, payload: dict[str, Any]
    ) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(payload)
        except Exception as exc:
            logger.warning("chat_session_ingestion progress callback failed: %s", exc)

    @staticmethod
    def _source_profile_authority_blocked(warnings: Sequence[str]) -> bool:
        return any(
            isinstance(warning, str)
            and warning.startswith(
                (
                    "source_profile_authority_missing:",
                    "source_profile_authority_bootstrap_failed:",
                    "source_profile_authority_validation_failed:",
                )
            )
            for warning in warnings
        )

    @staticmethod
    def _counters_to_dict(counters: SyncCounters) -> dict[str, int]:
        return {
            "discovered": counters.discovered,
            "classified_new": counters.classified_new,
            "classified_updated": counters.classified_updated,
            "classified_unchanged": counters.classified_unchanged,
            "created": counters.created,
            "updated": counters.updated,
            "skipped": counters.skipped,
            "failed": counters.failed,
            "intended_mutations": counters.intended_mutations,
            "executed_mutations": counters.executed_mutations,
        }

    def _emit_record_processed_progress(
        self,
        *,
        progress_callback: ProgressCallback | None,
        dry_run: bool,
        index: int,
        total: int,
        decision: SessionDecision,
        result_entry: dict[str, Any],
        counters: SyncCounters,
    ) -> None:
        self._emit_progress(
            progress_callback,
            {
                "event": "record_processed",
                "at_utc": _utc_now_iso(),
                "dry_run": dry_run,
                "index": index,
                "total": total,
                "environment": decision.record.environment,
                "source_session_id": decision.record.source_session_id,
                "document_concept_id": decision.document_concept_id,
                "action": result_entry.get("action"),
                "reason": result_entry.get("reason"),
                "success": bool(result_entry.get("success")),
                "file_copy_concept_id": result_entry.get("file_copy_concept_id"),
                "storage_object_written": bool(
                    result_entry.get("storage_object_written")
                ),
                "storage_backend": result_entry.get("storage_backend"),
                "storage_key": result_entry.get("storage_key"),
                "storage_uri": result_entry.get("storage_uri"),
                "ontology_links_aligned": result_entry.get("ontology_links_aligned"),
                "ontology_type_aligned": result_entry.get("ontology_type_aligned"),
                "context_links_aligned": result_entry.get("context_links_aligned"),
                "attached_context_bundle_ids": result_entry.get(
                    "attached_context_bundle_ids"
                ),
                "attached_context_dossier_ids": result_entry.get(
                    "attached_context_dossier_ids"
                ),
                "backup_written": result_entry.get("backup_written"),
                "backup_reused_existing": result_entry.get("backup_reused_existing"),
                "backup_path": result_entry.get("backup_path")
                or result_entry.get("planned_backup_path"),
                "counters": self._counters_to_dict(counters),
            },
        )

    def discover_records(self) -> tuple[list[SessionRecord], list[str]]:
        warnings: list[str] = []
        records: list[SessionRecord] = []
        seen: set[tuple[str, str, str]] = set()
        self._ensure_default_adapters()
        if self._default_adapter_warnings:
            warnings.extend(self._default_adapter_warnings)
            self._default_adapter_warnings = []

        for adapter in self.adapters:
            try:
                adapter_records, adapter_warnings = adapter.discover()
                warnings.extend(adapter_warnings)
            except Exception as exc:
                warnings.append(
                    f"{getattr(adapter, 'environment', 'unknown')}: adapter failed ({exc})"
                )
                continue

            for record in adapter_records:
                key = record.idempotency_key
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)

        records.sort(key=lambda rec: (rec.environment, rec.canonical_source_path))
        return records, warnings

    def _classify_record(self, record: SessionRecord) -> SessionDecision:
        document_concept_id = record.document_concept_id
        existing = self._get_concept(document_concept_id)
        if not existing:
            return SessionDecision(
                action="new",
                reason="document_not_found",
                record=record,
                document_concept_id=document_concept_id,
            )

        existing_hash = self._read_attribute(existing, "source_content_sha256")
        if isinstance(existing_hash, str) and existing_hash == record.content_sha256:
            repair_context = self._repair_context_for_existing_document(existing)
            if repair_context is not None:
                return SessionDecision(
                    action="repair",
                    reason=str(repair_context.get("reason") or "link_repair_required"),
                    record=record,
                    document_concept_id=document_concept_id,
                    repair_file_copy_concept_id=repair_context.get(
                        "file_copy_concept_id"
                    ),
                )
            return SessionDecision(
                action="unchanged",
                reason="content_hash_match",
                record=record,
                document_concept_id=document_concept_id,
            )

        return SessionDecision(
            action="updated",
            reason="content_hash_changed",
            record=record,
            document_concept_id=document_concept_id,
        )

    def _repair_context_for_existing_document(
        self, concept_doc: dict[str, Any]
    ) -> dict[str, str | None] | None:
        linked_ids = self._linked_file_copy_ids(concept_doc)
        canonical_linked_ids = self._linked_file_copy_ids_for_predicate(
            concept_doc, self._document_has_file_predicate_id()
        )
        current_file_copy = self._read_attribute(
            concept_doc, "current_file_copy_concept_id"
        )
        current_file_copy_id = (
            current_file_copy.strip()
            if isinstance(current_file_copy, str) and current_file_copy.strip()
            else None
        )

        if current_file_copy_id and current_file_copy_id not in linked_ids:
            return {
                "reason": "current_file_copy_not_linked",
                "file_copy_concept_id": current_file_copy_id,
            }

        if linked_ids and not canonical_linked_ids:
            preferred = (
                current_file_copy_id
                if current_file_copy_id and current_file_copy_id in linked_ids
                else linked_ids[0]
            )
            return {
                "reason": "canonical_link_missing",
                "file_copy_concept_id": preferred,
            }

        if not linked_ids and current_file_copy_id:
            return {
                "reason": "document_link_missing",
                "file_copy_concept_id": current_file_copy_id,
            }

        if linked_ids and not current_file_copy_id:
            return {
                "reason": "current_file_copy_metadata_missing",
                "file_copy_concept_id": linked_ids[0],
            }

        if not self._context_links_aligned(concept_doc):
            preferred = (
                current_file_copy_id
                if current_file_copy_id and current_file_copy_id in linked_ids
                else (linked_ids[0] if linked_ids else current_file_copy_id)
            )
            return {
                "reason": "requested_context_links_missing",
                "file_copy_concept_id": preferred,
            }

        return None

    def _apply_new(
        self, record: SessionRecord, document_concept_id: str
    ) -> dict[str, Any]:
        profile = self._config_for(record.environment)
        file_import = self._import_record_file(
            record,
            profile.file_copy_type.concept_id,
            profile.source_system,
        )
        if not file_import.get("success"):
            return {
                "success": False,
                "error": "file_import_failed",
                "details": file_import,
            }

        file_copy_concept_id = str(file_import.get("concept_id") or "").strip()
        if not file_copy_concept_id:
            return {"success": False, "error": "missing_file_copy_concept_id"}
        storage = file_import.get("storage")
        if not isinstance(storage, dict):
            storage = {}

        title = record.title or Path(record.local_path).name
        existing = self._get_concept(document_concept_id)
        document_concept_created = False
        if not existing:
            self._create_document_concept(
                concept_id=document_concept_id,
                name=title,
                document_type_id=profile.document_type.concept_id,
            )
            document_concept_created = True

        self._ensure_document_type(
            document_concept_id, profile.document_type.concept_id
        )
        self._ensure_link_pair(document_concept_id, file_copy_concept_id)
        self._update_document_metadata(
            document_concept_id=document_concept_id,
            record=record,
            current_file_copy_concept_id=file_copy_concept_id,
            previous_file_copy_concept_ids=[],
            source_system=profile.source_system,
        )
        context_alignment = self._ensure_requested_context_links(document_concept_id)
        return {
            "success": True,
            "file_copy_concept_id": file_copy_concept_id,
            "document_concept_created": document_concept_created,
            "storage_object_written": bool(storage.get("key")),
            "storage_backend": storage.get("backend"),
            "storage_key": storage.get("key"),
            "storage_uri": storage.get("uri"),
            "ontology_links_aligned": self._is_document_file_link_aligned(
                document_concept_id, file_copy_concept_id
            ),
            "ontology_type_aligned": self._is_document_type_aligned(
                document_concept_id, profile.document_type.concept_id
            ),
            **context_alignment,
        }

    def _apply_update(
        self, record: SessionRecord, document_concept_id: str
    ) -> dict[str, Any]:
        profile = self._config_for(record.environment)
        existing = self._get_concept(document_concept_id)
        if not existing:
            return {"success": False, "error": "document_missing_for_update"}

        old_file_copy_ids = self._linked_file_copy_ids(existing)
        file_import = self._import_record_file(
            record,
            profile.file_copy_type.concept_id,
            profile.source_system,
        )
        if not file_import.get("success"):
            return {
                "success": False,
                "error": "file_import_failed",
                "details": file_import,
            }

        new_file_copy_concept_id = str(file_import.get("concept_id") or "").strip()
        if not new_file_copy_concept_id:
            return {"success": False, "error": "missing_file_copy_concept_id"}
        storage = file_import.get("storage")
        if not isinstance(storage, dict):
            storage = {}

        self._ensure_document_type(
            document_concept_id, profile.document_type.concept_id
        )
        for old_id in old_file_copy_ids:
            if old_id == new_file_copy_concept_id:
                continue
            self._remove_link_pair(document_concept_id, old_id)

        self._ensure_link_pair(document_concept_id, new_file_copy_concept_id)
        self._update_document_metadata(
            document_concept_id=document_concept_id,
            record=record,
            current_file_copy_concept_id=new_file_copy_concept_id,
            previous_file_copy_concept_ids=old_file_copy_ids,
            source_system=profile.source_system,
        )
        context_alignment = self._ensure_requested_context_links(document_concept_id)

        return {
            "success": True,
            "file_copy_concept_id": new_file_copy_concept_id,
            "replaced_file_copy_concept_ids": old_file_copy_ids,
            "storage_object_written": bool(storage.get("key")),
            "storage_backend": storage.get("backend"),
            "storage_key": storage.get("key"),
            "storage_uri": storage.get("uri"),
            "ontology_links_aligned": self._is_document_file_link_aligned(
                document_concept_id, new_file_copy_concept_id
            ),
            "ontology_type_aligned": self._is_document_type_aligned(
                document_concept_id, profile.document_type.concept_id
            ),
            **context_alignment,
        }

    def _apply_repair(
        self,
        record: SessionRecord,
        document_concept_id: str,
        preferred_file_copy_concept_id: str | None,
    ) -> dict[str, Any]:
        profile = self._config_for(record.environment)
        existing = self._get_concept(document_concept_id)
        if not existing:
            return {"success": False, "error": "document_missing_for_repair"}

        linked_ids = self._linked_file_copy_ids(existing)
        preferred = (
            preferred_file_copy_concept_id.strip()
            if isinstance(preferred_file_copy_concept_id, str)
            and preferred_file_copy_concept_id.strip()
            else None
        )
        if preferred is None:
            attr_current = self._read_attribute(
                existing, "current_file_copy_concept_id"
            )
            if isinstance(attr_current, str) and attr_current.strip():
                preferred = attr_current.strip()
        if preferred is None and linked_ids:
            preferred = linked_ids[0]
        if preferred is None:
            return {"success": False, "error": "missing_file_copy_reference_for_repair"}

        self._ensure_document_type(
            document_concept_id, profile.document_type.concept_id
        )
        self._ensure_link_pair(document_concept_id, preferred)

        previous_ids = [file_id for file_id in linked_ids if file_id != preferred]
        self._update_document_metadata(
            document_concept_id=document_concept_id,
            record=record,
            current_file_copy_concept_id=preferred,
            previous_file_copy_concept_ids=previous_ids,
            source_system=profile.source_system,
        )
        context_alignment = self._ensure_requested_context_links(document_concept_id)

        return {
            "success": True,
            "file_copy_concept_id": preferred,
            "repaired_from_file_copy_concept_ids": linked_ids,
            "storage_object_written": False,
            "storage_backend": None,
            "storage_key": None,
            "storage_uri": None,
            "ontology_links_aligned": self._is_document_file_link_aligned(
                document_concept_id, preferred
            ),
            "ontology_type_aligned": self._is_document_type_aligned(
                document_concept_id, profile.document_type.concept_id
            ),
            **context_alignment,
        }

    def _import_record_file(
        self,
        record: SessionRecord,
        file_copy_type_id: str,
        source_system: str,
    ) -> dict[str, Any]:
        return import_local_file_copy(
            local_path=record.local_path,
            user_concept_id=self.user_concept_id,
            type_concept_id=file_copy_type_id,
            source_system=source_system,
            source_identifier=record.source_session_id,
            source_uri=record.source_uri,
        )

    def _create_document_concept(
        self,
        *,
        concept_id: str,
        name: str,
        document_type_id: str,
    ) -> None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            parent_concept_ids=[document_type_id],
            create_as_instance=True,
            system_tags=["ai_assisted_programming", "chat_session"],
            attributes={"created_by_sync_script": True},
        )
        concept_service.update_concept(
            concept_id,
            {
                f"relationships.{CANONICAL_SPECIFIC_TO_USER_PREDICATE}": [
                    self.user_concept_id
                ]
            },
        )

    def _ensure_document_type(
        self, document_concept_id: str, document_type_id: str
    ) -> None:
        result = add_relationship(
            document_concept_id,
            self._instance_of_predicate_id(),
            document_type_id,
        )
        if not result.get("success"):
            raise RuntimeError(
                f"failed_to_set_document_type:{document_concept_id}:{document_type_id}:{result}"
            )

    def _ensure_link_pair(
        self, document_concept_id: str, file_copy_concept_id: str
    ) -> None:
        doc_to_file = add_relationship(
            document_concept_id,
            self._document_has_file_predicate_id(),
            file_copy_concept_id,
        )
        if not doc_to_file.get("success"):
            raise RuntimeError(
                f"failed_link_document_to_file:{document_concept_id}:{file_copy_concept_id}:{doc_to_file}"
            )

        file_to_doc = add_relationship(
            file_copy_concept_id,
            self._file_for_document_predicate_id(),
            document_concept_id,
        )
        if not file_to_doc.get("success"):
            raise RuntimeError(
                f"failed_link_file_to_document:{file_copy_concept_id}:{document_concept_id}:{file_to_doc}"
            )

    def _remove_link_pair(
        self, document_concept_id: str, file_copy_concept_id: str
    ) -> None:
        for predicate in (
            self._document_has_file_predicate_id(),
            self._legacy_document_has_file_copy_predicate_id(),
        ):
            remove_relationship(
                source_id=document_concept_id,
                predicate=predicate,
                target=file_copy_concept_id,
                confirmed=True,
            )
        remove_relationship(
            source_id=file_copy_concept_id,
            predicate=self._file_for_document_predicate_id(),
            target=document_concept_id,
            confirmed=True,
        )

    def _update_document_metadata(
        self,
        *,
        document_concept_id: str,
        record: SessionRecord,
        current_file_copy_concept_id: str,
        previous_file_copy_concept_ids: Sequence[str],
        source_system: str,
    ) -> None:
        update_data: dict[str, Any] = {
            "attributes.source_environment": record.environment,
            "attributes.source_system": source_system,
            "attributes.source_session_id": record.source_session_id,
            "attributes.source_path": record.canonical_source_path,
            "attributes.source_uri": record.source_uri,
            "attributes.source_modified_at_utc": record.source_modified_at_utc,
            "attributes.source_size_bytes": record.source_size_bytes,
            "attributes.source_content_sha256": record.content_sha256,
            "attributes.current_file_copy_concept_id": current_file_copy_concept_id,
            "attributes.sync_last_ingested_at_utc": _utc_now_iso(),
        }
        if record.title:
            update_data["attributes.source_title"] = record.title
        if previous_file_copy_concept_ids:
            old_ids = [
                cid for cid in previous_file_copy_concept_ids if isinstance(cid, str)
            ]
            update_data["attributes.previous_file_copy_concept_ids"] = sorted(
                set(old_ids)
            )
        concept_service.update_concept(document_concept_id, update_data)

    @staticmethod
    def _read_attribute(concept_doc: dict[str, Any], key: str) -> Any:
        attrs = concept_doc.get("attributes")
        if not isinstance(attrs, dict):
            return None
        return attrs.get(key)

    def _linked_file_copy_ids(self, concept_doc: dict[str, Any]) -> list[str]:
        linked_ids: list[str] = []
        for predicate in (
            self._document_has_file_predicate_id(),
            self._legacy_document_has_file_copy_predicate_id(),
        ):
            linked_ids.extend(
                AIChatSessionIngestionService._linked_file_copy_ids_for_predicate(
                    concept_doc, predicate
                )
            )
        return sorted(set(linked_ids))

    @staticmethod
    def _linked_file_copy_ids_for_predicate(
        concept_doc: dict[str, Any], predicate: str
    ) -> list[str]:
        relationships = concept_doc.get("relationships")
        if not isinstance(relationships, dict):
            return []
        raw = relationships.get(predicate)
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str)]
        return []

    def _is_document_file_link_aligned(
        self, document_concept_id: str, file_copy_concept_id: str
    ) -> bool:
        document = self._get_concept(document_concept_id)
        if not isinstance(document, dict):
            return False
        linked_ids = self._linked_file_copy_ids_for_predicate(
            document,
            self._document_has_file_predicate_id(),
        )
        if file_copy_concept_id not in linked_ids:
            return False
        file_copy = self._get_concept(file_copy_concept_id)
        if not isinstance(file_copy, dict):
            return False
        relationships = file_copy.get("relationships")
        if not isinstance(relationships, dict):
            return False
        inverse = relationships.get(self._file_for_document_predicate_id())
        if isinstance(inverse, str):
            return inverse == document_concept_id
        if isinstance(inverse, list):
            return document_concept_id in inverse
        return False

    def _is_document_type_aligned(
        self, document_concept_id: str, document_type_id: str
    ) -> bool:
        document = self._get_concept(document_concept_id)
        if not isinstance(document, dict):
            return False
        relationships = document.get("relationships")
        if not isinstance(relationships, dict):
            return False
        raw = relationships.get(self._instance_of_predicate_id())
        if raw is None:
            raw = relationships.get("is_an_instance_of")
        if isinstance(raw, str):
            return raw == document_type_id
        if isinstance(raw, list):
            return document_type_id in raw
        return False

    @staticmethod
    def _get_concept(concept_id: str) -> dict[str, Any] | None:
        try:
            doc = concept_service.get_concept_by_concept_id(concept_id)
            return doc if isinstance(doc, dict) else None
        except concept_service.ConceptNotFoundError:
            return None

    def _config_for(self, environment: str) -> AIChatSessionSourceProfile:
        try:
            return self._load_source_profile_authority().profile_for_environment(
                environment
            )
        except KeyError as exc:
            raise ValueError(
                f"unsupported represented environment: {environment}"
            ) from exc

    def _document_has_file_predicate_id(self) -> str:
        return (
            self._load_source_profile_authority().document_has_file_predicate.concept_id
        )

    def _legacy_document_has_file_copy_predicate_id(self) -> str:
        return (
            self._load_source_profile_authority().legacy_document_has_file_copy_predicate.concept_id
        )

    def _file_for_document_predicate_id(self) -> str:
        return (
            self._load_source_profile_authority().file_for_document_predicate.concept_id
        )

    def _instance_of_predicate_id(self) -> str:
        return self._load_source_profile_authority().instance_of_predicate_id


def run_ingestion(
    *,
    user_concept_id: str,
    dry_run: bool = True,
    limit: int | None = None,
    adapters: Sequence[SessionSourceAdapter] | None = None,
    progress_callback: ProgressCallback | None = None,
    context_bundle_ids: Sequence[str] = (),
    context_dossier_ids: Sequence[str] = (),
    backup_root: Path | None = None,
) -> dict[str, Any]:
    service = AIChatSessionIngestionService(
        user_concept_id=user_concept_id,
        adapters=adapters,
        context_bundle_ids=context_bundle_ids,
        context_dossier_ids=context_dossier_ids,
        backup_root=backup_root,
    )
    result = service.run(
        dry_run=dry_run,
        limit=limit,
        progress_callback=progress_callback,
    )
    payload = result.to_dict()
    logger.info("ai_chat_session_ingestion_summary=%s", json.dumps(payload))
    return payload


__all__ = [
    "AIChatSessionIngestionService",
    "SessionRecord",
    "SyncResult",
    "GeminiSessionAdapter",
    "run_ingestion",
    "stable_document_concept_id",
]
