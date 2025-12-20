"""Safely inspect selected values from `.env`.

This utility exists to support local debugging without accidentally printing
high-value secrets (API tokens, passwords, etc.). It implements a strict
allowlist and redacts values that look secret-like.

Usage (PowerShell):
    pdm run python utilities/get_safe_env.py VON_DB_NAME
    pdm run python utilities/get_safe_env.py MONGO_URI

Exit codes:
    0 - ok
    2 - key not allowlisted
    3 - key not found
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Optional


SAFE_KEYS = {
    # Database config
    "VON_DB_NAME",
    "MONGO_PROJECT",
    "MONGO_ALLOW_LOCAL_FALLBACK",
    "MONGO_LOCAL_URI",
    "MONGO_URI",
    # Non-secret feature toggles
    "VON_INTERNAL_MCP_ENABLE",
    "VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS",
    "VON_DEBUG_MONGO",
    "OLLAMA_HOSTS_LIST",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS",
}


_SECRET_KEY_PATTERNS = re.compile(
    r"(API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE|OPENAI|ATLASSIAN|OS_APPLICATION_CREDENTIAL)",
    re.IGNORECASE,
)


def _parse_env_file(env_path: Path) -> Dict[str, str]:
    """Parse a minimal .env file into a dict.

    This intentionally avoids executing anything and ignores `export`.
    """

    values: Dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.lower().startswith("export "):
            line = line[7:].lstrip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        # Strip surrounding quotes
        if len(value) >= 2 and (
            (value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")
        ):
            value = value[1:-1]

        values[key] = value

    return values


def _redact_value(key: str, value: str) -> str:
    # Always redact if the key itself suggests secrets.
    if _SECRET_KEY_PATTERNS.search(key):
        return "[REDACTED]"

    # Redact credentials in Mongo-style URIs.
    if key in {"MONGO_URI", "MONGO_LOCAL_URI"}:
        # mongodb+srv://user:pass@host/...  -> mongodb+srv://user:[REDACTED]@host/...
        return re.sub(r"(mongodb\+srv://[^:]+:)([^@]+)(@)", r"\1[REDACTED]\3", value)

    # Heuristic: long opaque values are probably secrets.
    if len(value) >= 40 and re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        return value[:6] + "…" + value[-4:]

    return value


def get_safe_env_value(env_path: Path, key: str) -> Optional[str]:
    if key not in SAFE_KEYS:
        return None

    values = _parse_env_file(env_path)
    if key not in values:
        return ""

    return _redact_value(key, values[key])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely print allowlisted .env values (redacted)."
    )
    parser.add_argument("key", help="Environment variable key to display")
    parser.add_argument(
        "--env-path",
        default=str(Path(__file__).resolve().parents[1] / ".env"),
        help="Path to the .env file (default: repo root .env)",
    )

    args = parser.parse_args(argv)
    key = str(args.key).strip()
    env_path = Path(args.env_path)

    if key not in SAFE_KEYS:
        print(f"Key '{key}' is not allowlisted.")
        return 2

    values = _parse_env_file(env_path)
    if key not in values:
        print(f"Key '{key}' not found in {env_path}.")
        return 3

    print(_redact_value(key, values[key]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
