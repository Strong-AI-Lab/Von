from __future__ import annotations

import json
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet

from scripts import restore_von_db


def _make_dump_tree(tmp_path: Path, *, db_name: str = "von_db") -> Path:
    backup_root = tmp_path / "backup_root"
    db_dir = backup_root / db_name
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "papers.bson").write_bytes(b"paper-data")
    (db_dir / "papers.metadata.json").write_text("{}", encoding="utf-8")
    return backup_root


def _write_receipt(path: Path, artifact_path: Path, *, db_name: str = "von_db") -> None:
    payload = {
        "schema_version": "backup_success_receipt.v1",
        "completed_at_utc": "2026-04-06T12:00:00Z",
        "db_name": db_name,
        "tag": "auto-daily",
        "out_root": str(artifact_path.parent.resolve()),
        "backup_root": str(artifact_path.resolve()),
        "final_artifact_path": str(artifact_path.resolve()),
        "artifact_kind": "directory" if artifact_path.is_dir() else "zip",
        "compressed": artifact_path.suffix == ".zip",
        "encrypted": artifact_path.name.endswith(".enc"),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _zip_backup(source_root: Path, destination_zip: Path) -> Path:
    with zipfile.ZipFile(destination_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in source_root.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(source_root).as_posix())
    return destination_zip


def test_dry_run_from_receipt_defaults_to_probe_target(
    tmp_path: Path, capsys
) -> None:
    backup_root = _make_dump_tree(tmp_path)
    receipt_path = tmp_path / "launcher_receipt.json"
    _write_receipt(receipt_path, backup_root)

    exit_code = restore_von_db.main(["--backup-path", str(receipt_path)])

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert f"[restore] Backup input: {receipt_path.resolve()}" in captured
    assert f"[restore] Final artefact: {backup_root.resolve()}" in captured
    assert "[restore] Source DB: von_db" in captured
    assert "[restore] Target DB: von_db_restore_probe" in captured
    assert "[restore] Dump summary: collections=1 drop_target=False" in captured


def test_apply_from_zip_runs_mongorestore_with_namespace_remap(
    tmp_path: Path, monkeypatch
) -> None:
    backup_root = _make_dump_tree(tmp_path)
    zip_path = _zip_backup(backup_root, tmp_path / "backup.zip")
    captured: dict[str, object] = {}

    def _fake_run_mongorestore(
        *,
        mongo_uri: str,
        dump_root: Path,
        source_db_name: str,
        target_db_name: str,
        drop_target: bool,
    ) -> None:
        captured["mongo_uri"] = mongo_uri
        captured["dump_root"] = dump_root
        captured["source_db_name"] = source_db_name
        captured["target_db_name"] = target_db_name
        captured["drop_target"] = drop_target
        assert (dump_root / source_db_name / "papers.bson").exists()

    monkeypatch.setattr(restore_von_db, "_run_mongorestore", _fake_run_mongorestore)

    exit_code = restore_von_db.main(
        [
            "--backup-path",
            str(zip_path),
            "--target-db-name",
            "von_db_restore_probe",
            "--apply",
            "--drop-target",
        ]
    )

    assert exit_code == 0
    assert captured["source_db_name"] == "von_db"
    assert captured["target_db_name"] == "von_db_restore_probe"
    assert captured["drop_target"] is True
    assert str(captured["mongo_uri"]).startswith("mongodb://")


def test_dry_run_from_encrypted_zip_uses_fernet_key(
    tmp_path: Path, monkeypatch
) -> None:
    backup_root = _make_dump_tree(tmp_path)
    zip_path = _zip_backup(backup_root, tmp_path / "backup.zip")
    key = Fernet.generate_key()
    encrypted_path = tmp_path / "backup.zip.enc"
    encrypted_path.write_bytes(Fernet(key).encrypt(zip_path.read_bytes()))
    monkeypatch.setenv("VON_BACKUP_ENCRYPTION_KEY", key.decode("utf-8"))

    exit_code = restore_von_db.main(["--backup-path", str(encrypted_path)])

    assert exit_code == 0
