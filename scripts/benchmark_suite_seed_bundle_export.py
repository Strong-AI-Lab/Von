from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
repo_root_str = str(REPO_ROOT)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)

from src.backend.services.benchmark_suite_vontology_service import (  # noqa: E402
    load_benchmark_suite_definition,
)

_AUTHORITY_FIELDS = (
    "definition_schema_version",
    "schema_version",
    "seed_schema_version",
    "seed_version",
    "suite_id",
    "suite_concept_id",
    "suite_title",
    "suite_description",
    "default_case_set",
    "case_sets",
    "rubric",
)
_LEGACY_DIGEST_FIELD = "known_legacy_authority_payload_sha256_by_seed_version"


def _resolve_path(raw_value: str) -> Path:
    return Path(raw_value).expanduser().resolve()


def _existing_transport_metadata(target_path: Path) -> dict[str, Any]:
    if not target_path.exists():
        return {}
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("benchmark_suite_seed_bundle_not_mapping")
    return (
        {_LEGACY_DIGEST_FIELD: payload.get(_LEGACY_DIGEST_FIELD)}
        if payload.get(_LEGACY_DIGEST_FIELD) is not None
        else {}
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export a generated repo-seed benchmark bundle from authoritative "
            "Vontology suite state."
        )
    )
    parser.add_argument("--suite-id", required=True)
    parser.add_argument("--asset-path", required=True)
    args = parser.parse_args(argv)

    suite_id = str(args.suite_id or "").strip()
    if not suite_id.startswith("#V#"):
        raise ValueError("benchmark_suite_concept_id_invalid")
    target_path = _resolve_path(args.asset_path)
    definition, diagnostics = load_benchmark_suite_definition(suite_id)
    payload = {
        key: definition.get(key)
        for key in _AUTHORITY_FIELDS
        if definition.get(key) is not None
    }
    payload.update(_existing_transport_metadata(target_path))
    rendered = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    target_path.write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "asset_path": str(target_path),
                "bytes_written": len(rendered.encode("utf-8")),
                "definition_sha256": diagnostics.get("definition_sha256"),
                "suite_id": suite_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
