#!/usr/bin/env python3
"""Regenerate src/backend/mcp_server/vontology_mcp.json from canonical contracts."""

from __future__ import annotations

import json
from pathlib import Path

from src.backend.integrations.internal_mcp.tool_contract_registry import (
    get_manifest_payload,
)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest_path = project_root / "src" / "backend" / "mcp_server" / "vontology_mcp.json"
    payload = get_manifest_payload()
    manifest_path.write_text(
        json.dumps(payload, indent=4, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Regenerated {manifest_path} with {len(payload.get('tools', []))} tools."
    )


if __name__ == "__main__":
    main()
