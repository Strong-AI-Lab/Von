"""Back up retained task/project files, or restore them beside a restored database.

Complements restore_von_db.py: MongoDB contains file-copy identities, while the
original bytes live in blob storage. No Jira connection is needed for either
operation. Run export against the restored snapshot to bind its exact file set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def file_copy_ids(value):
    ids = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "file_copy_concept_id" and isinstance(item, str):
                ids.add(item)
            else:
                ids.update(file_copy_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(file_copy_ids(item))
    return ids


def export_files(*, project_ids, destination, actor_id, database_receipt, cipher):
    from src.backend.db.mongo_client import get_configured_database_name
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.computer_file_copy_service import (
        fetch_file_copy_bytes,
        resolve_file_copy_blob_info,
    )
    from src.backend.services.task_project_service import (
        get_task_project,
        list_project_collections,
    )

    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifiers = set()
    for project_id in project_ids:
        project = get_task_project(project_id)
        identifiers.update(file_copy_ids(project))
        identifiers.update(file_copy_ids(list_project_collections(project)))
    tasks = list(
        ConceptsRepository.find(
            {"metadata.project_concept_id": {"$in": project_ids}},
            projection={"concept_id": 1, "metadata": 1},
            limit=0,
        )
    )
    identifiers.update(file_copy_ids(tasks))
    manifest = {
        "schema": "task_project_file_backup.v1",
        "source_database": database_receipt["db_name"],
        "export_database": get_configured_database_name(),
        "database_receipt": database_receipt,
        "project_concept_ids": project_ids,
        "task_count": len(tasks),
        "files": [],
        "complete": False,
    }
    for identifier in sorted(identifiers):
        result = fetch_file_copy_bytes(
            file_copy_concept_id=identifier, user_concept_id=actor_id, allow_large=True
        )
        if not result.get("success"):
            raise ValueError(f"Cannot back up referenced file copy: {identifier}")
        data = result["data"]
        digest = hashlib.sha256(data).hexdigest()
        encrypted_path = destination / f"{digest}.bin.enc"
        encrypted_path.write_bytes(cipher.encrypt(data))
        os.chmod(encrypted_path, 0o600)
        if (
            hashlib.sha256(cipher.decrypt(encrypted_path.read_bytes())).hexdigest()
            != digest
        ):
            raise ValueError("Encrypted backup read-back failed")
        info = resolve_file_copy_blob_info(file_copy_concept_id=identifier)
        manifest["files"].append(
            {
                "file_copy_concept_id": identifier,
                "sha256": digest,
                "size_bytes": len(data),
                "original_filename": info.original_filename,
                "content_type": info.content_type,
            }
        )
        print(
            json.dumps({"backed_up": identifier, "size_bytes": len(data)}), flush=True
        )
    manifest["complete"] = True
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def restore_files(*, backup_dir, destination, actor_id, cipher):
    from src.backend.db.mongo_client import get_configured_database_name
    from src.backend.services.blob_store import LocalBlobStore
    from src.backend.services.computer_file_copy_service import fetch_file_copy_bytes
    from src.backend.services.concept_service import update_concept

    manifest = json.loads((backup_dir / "manifest.json").read_text())
    target_db = get_configured_database_name()
    if target_db in {"von_db", manifest["source_database"]}:
        raise ValueError("File restore requires a separate restored database")
    if not manifest.get("complete"):
        raise ValueError("File backup is incomplete")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = LocalBlobStore(destination)
    os.environ["VON_BLOB_STORE_LOCAL_ROOT"] = str(destination.resolve())
    os.environ["VON_BLOB_STORE_BACKEND"] = "local"
    restored = []
    for entry in manifest["files"]:
        digest = entry["sha256"]
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Invalid file digest")
        data = cipher.decrypt((backup_dir / f"{digest}.bin.enc").read_bytes())
        if (
            len(data) != entry["size_bytes"]
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("File backup integrity mismatch")
        ref = store.put_bytes(
            f"restored-task-files/{digest}", data, content_type=entry["content_type"]
        )
        identifier = entry["file_copy_concept_id"]
        update_concept(
            identifier,
            {
                "attributes.file_copy_metadata_storage": "attributes.v1",
                "attributes.blob_backend": "local",
                "attributes.blob_key": ref.key,
                "attributes.blob_uri": ref.uri,
                "attributes.size_bytes": len(data),
                "attributes.original_filename": entry["original_filename"],
                "attributes.content_type": entry["content_type"],
            },
        )
        result = fetch_file_copy_bytes(
            file_copy_concept_id=identifier, user_concept_id=actor_id, allow_large=True
        )
        if (
            not result.get("success")
            or hashlib.sha256(result["data"]).hexdigest() != digest
        ):
            raise ValueError(f"Restored file canonical read-back failed: {identifier}")
        restored.append(identifier)
    receipt = {
        "schema": "task_project_file_restore.v1",
        "target_database": target_db,
        "project_concept_ids": manifest["project_concept_ids"],
        "task_count": manifest["task_count"],
        "restored_file_copy_ids": restored,
        "complete": True,
    }
    (destination / "restore-receipt.json").write_text(json.dumps(receipt, indent=2))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "restore"])
    parser.add_argument("--actor-concept-id", required=True)
    parser.add_argument("--organisation-concept-id")
    parser.add_argument("--project-concept-id", action="append", default=[])
    parser.add_argument("--database-receipt", type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--restore-dir", type=Path)
    args = parser.parse_args()
    from cryptography.fernet import Fernet

    from src.backend.security.access_control import (
        force_access_control_enforcement,
        override_current_actor,
    )

    cipher = Fernet(os.environ["VON_BACKUP_ENCRYPTION_KEY"].encode())
    with (
        override_current_actor(args.actor_concept_id, args.organisation_concept_id),
        force_access_control_enforcement(),
    ):
        if args.operation == "export":
            if not args.project_concept_id or not args.database_receipt:
                parser.error(
                    "Export requires project IDs and the database backup receipt"
                )
            result = export_files(
                project_ids=args.project_concept_id,
                destination=args.backup_dir,
                actor_id=args.actor_concept_id,
                cipher=cipher,
                database_receipt=json.loads(args.database_receipt.read_text()),
            )
        else:
            if not args.restore_dir:
                parser.error("Restore requires a separate destination directory")
            result = restore_files(
                backup_dir=args.backup_dir,
                destination=args.restore_dir,
                actor_id=args.actor_concept_id,
                cipher=cipher,
            )
    print(
        json.dumps(
            {
                "complete": result["complete"],
                "file_count": len(
                    result.get("files", result.get("restored_file_copy_ids", []))
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
