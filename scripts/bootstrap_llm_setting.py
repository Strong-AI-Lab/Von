#!/usr/bin/env python3
"""Seed a Von user/org LLM setting during managed host bootstrap.

This is deployment support plumbing. It does not choose policy for the product;
it records an operator-supplied model setting via the existing settings service.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SUPPORTED_PROVIDERS = {"openai", "openrouter", "gemini", "meta", "ollama"}


def _normalise_concept_id(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if not value.startswith("#V#"):
        value = f"#V#{value}"
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed Von's scoped LLM setting after cloud bootstrap."
    )
    parser.add_argument("--provider", required=True, help="LLM provider name.")
    parser.add_argument("--model", required=True, help="Model identifier.")
    parser.add_argument("--user-concept-id", help="Optional user concept id.")
    parser.add_argument(
        "--organisation-concept-id",
        help="Optional organisation concept id.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    provider = args.provider.strip().lower()
    model = args.model.strip()
    user_concept_id = _normalise_concept_id(args.user_concept_id)
    organisation_concept_id = _normalise_concept_id(args.organisation_concept_id)

    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        parser.error(f"--provider must be one of: {supported}")
    if not model:
        parser.error("--model must not be empty")
    if not user_concept_id and not organisation_concept_id:
        parser.error("provide --user-concept-id or --organisation-concept-id")

    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    from src.backend.services.settings_service import (  # noqa: PLC0415
        set_org_llm_setting,
        set_user_llm_setting,
    )

    outcomes: dict[str, bool | str | None] = {
        "provider": provider,
        "model": model,
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
    }

    if organisation_concept_id:
        outcomes["organisation_setting_updated"] = set_org_llm_setting(
            organisation_concept_id,
            provider,
            model,
        )
    if user_concept_id:
        outcomes["user_setting_updated"] = set_user_llm_setting(
            user_concept_id,
            provider,
            model,
        )

    success_values = [
        value
        for key, value in outcomes.items()
        if key.endswith("_setting_updated")
    ]
    print(json.dumps(outcomes, sort_keys=True))
    return 0 if success_values and all(success_values) else 1


if __name__ == "__main__":
    raise SystemExit(main())
