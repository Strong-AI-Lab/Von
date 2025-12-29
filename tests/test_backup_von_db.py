from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

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
