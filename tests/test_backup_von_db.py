from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scripts import backup_von_db
from scripts.backup_von_db import _apply_retention_and_storage_limits


def _touch(path: Path, *, age_seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix:
        path.write_bytes(b"x" * 10)
    else:
        path.mkdir(parents=True, exist_ok=True)
        (path / "dummy.txt").write_text("x" * 10, encoding="utf-8")
    ts = time.time() - age_seconds
    os.utime(path, (ts, ts))


def test_retention_deletes_old_backups(tmp_path: Path) -> None:
    out_root = tmp_path / "backups"
    old_dir = out_root / "von_db_20000101_000000Z_auto-daily"
    new_dir = out_root / "von_db_20990101_000000Z_auto-daily"

    _touch(old_dir, age_seconds=60 * 60 * 24 * 10)
    _touch(new_dir, age_seconds=60)

    _apply_retention_and_storage_limits(
        out_root=out_root,
        retention_days=1,
        max_storage_mb=-1,
        protect_paths=set(),
    )

    assert not old_dir.exists()
    assert new_dir.exists()


def test_max_storage_deletes_oldest_first(tmp_path: Path) -> None:
    out_root = tmp_path / "backups"
    old_zip = out_root / "von_db_20000101_000000Z_auto-daily.zip"
    new_zip = out_root / "von_db_20990101_000000Z_auto-daily.zip"

    _touch(old_zip, age_seconds=60 * 60 * 24 * 10)
    _touch(new_zip, age_seconds=60)

    # 1 file is 10 bytes; set limit so only one can remain.
    _apply_retention_and_storage_limits(
        out_root=out_root,
        retention_days=-1,
        max_storage_mb=0,  # 0 MB forces deletion until <= 0 bytes (but protect new)
        protect_paths={new_zip},
    )

    assert new_zip.exists()
    # old must be deleted to try to satisfy storage cap
    assert not old_zip.exists()


def test_retention_cleanup_removes_deleted_artifact_sidecar(tmp_path: Path) -> None:
    out_root = tmp_path / "backups"
    old_zip = out_root / "von_db_20000101_000000Z_auto-daily.zip"
    old_sidecar = backup_von_db._backup_receipt_sidecar_path(old_zip)
    new_zip = out_root / "von_db_20990101_000000Z_auto-daily.zip"
    new_sidecar = backup_von_db._backup_receipt_sidecar_path(new_zip)

    _touch(old_zip, age_seconds=60 * 60 * 24 * 10)
    _touch(new_zip, age_seconds=60)
    old_sidecar.write_text("{}", encoding="utf-8")
    new_sidecar.write_text("{}", encoding="utf-8")

    _apply_retention_and_storage_limits(
        out_root=out_root,
        retention_days=1,
        max_storage_mb=-1,
        protect_paths={new_zip},
    )

    assert not old_zip.exists()
    assert not old_sidecar.exists()
    assert new_zip.exists()
    assert new_sidecar.exists()


def test_apply_backup_writes_receipts_only_after_validated_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "backups"
    launcher_receipt = tmp_path / ".run" / "last_successful_backup_receipt.json"
    legacy_sentinel = tmp_path / ".run" / "last_backup_utc.txt"

    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setattr(
        backup_von_db, "_utc_timestamp_compact", lambda: "20260406_010203Z"
    )
    monkeypatch.setattr(
        backup_von_db, "_utc_timestamp_iso", lambda: "2026-04-06T01:02:03Z"
    )

    def _fake_mongodump(*, mongo_uri: str, db_name: str, out_path: Path) -> None:
        db_path = out_path / db_name
        db_path.mkdir(parents=True, exist_ok=True)
        (db_path / "collection.bson").write_bytes(b"demo")

    monkeypatch.setattr(backup_von_db, "_run_mongodump", _fake_mongodump)

    exit_code = backup_von_db.main(
        [
            "--apply",
            "--out-dir",
            str(out_root),
            "--tag",
            "auto-daily",
            "--launcher-receipt-path",
            str(launcher_receipt),
            "--legacy-sentinel-path",
            str(legacy_sentinel),
        ]
    )

    artifact = out_root / "test_von_db_20260406_010203Z_auto-daily"
    sidecar = backup_von_db._backup_receipt_sidecar_path(artifact)

    assert exit_code == 0
    assert artifact.exists()
    assert launcher_receipt.exists()
    assert legacy_sentinel.read_text(encoding="utf-8").strip() == "2026-04-06T01:02:03Z"

    launcher_payload = json.loads(launcher_receipt.read_text(encoding="utf-8"))
    sidecar_payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert launcher_payload == sidecar_payload
    assert launcher_payload["schema_version"] == "backup_success_receipt.v1"
    assert launcher_payload["completed_at_utc"] == "2026-04-06T01:02:03Z"
    assert launcher_payload["db_name"] == "test_von_db"
    assert launcher_payload["tag"] == "auto-daily"
    assert launcher_payload["out_root"] == str(out_root.resolve())
    assert launcher_payload["backup_root"] == str(artifact.resolve())
    assert launcher_payload["final_artifact_path"] == str(artifact.resolve())
    assert launcher_payload["artifact_kind"] == "directory"
    assert launcher_payload["compressed"] is False
    assert launcher_payload["encrypted"] is False
    assert launcher_payload["collection_count"] == 1
    assert launcher_payload["artifact_size_bytes"] > 0


def test_apply_backup_failure_does_not_write_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "backups"
    launcher_receipt = tmp_path / ".run" / "last_successful_backup_receipt.json"
    legacy_sentinel = tmp_path / ".run" / "last_backup_utc.txt"

    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setattr(
        backup_von_db, "_utc_timestamp_compact", lambda: "20260406_040506Z"
    )
    monkeypatch.setattr(
        backup_von_db, "_utc_timestamp_iso", lambda: "2026-04-06T04:05:06Z"
    )

    def _raise_failure(*, mongo_uri: str, db_name: str, out_path: Path) -> None:
        raise RuntimeError("simulated mongodump failure")

    monkeypatch.setattr(backup_von_db, "_run_mongodump", _raise_failure)

    with pytest.raises(RuntimeError, match="simulated mongodump failure"):
        backup_von_db.main(
            [
                "--apply",
                "--out-dir",
                str(out_root),
                "--tag",
                "auto-daily",
                "--launcher-receipt-path",
                str(launcher_receipt),
                "--legacy-sentinel-path",
                str(legacy_sentinel),
            ]
        )

    artifact = out_root / "test_von_db_20260406_040506Z_auto-daily"
    sidecar = backup_von_db._backup_receipt_sidecar_path(artifact)

    assert artifact.exists()
    assert not sidecar.exists()
    assert not launcher_receipt.exists()
    assert not legacy_sentinel.exists()


@pytest.mark.parametrize(
    "retention_days,max_storage_mb",
    [(-1, -1), (30, -1), (-1, 100)],
)
def test_no_crash_on_empty(
    tmp_path: Path, retention_days: int, max_storage_mb: int
) -> None:
    out_root = tmp_path / "backups"
    _apply_retention_and_storage_limits(
        out_root=out_root,
        retention_days=retention_days,
        max_storage_mb=max_storage_mb,
        protect_paths=set(),
    )
