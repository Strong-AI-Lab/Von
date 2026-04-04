"""CLI entrypoint for the isolated KB-clone benchmark harness."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pymongo import MongoClient

from src.backend.services.kb_clone_benchmark_service import (
    BenchmarkHarnessError,
    build_default_benchmark_app,
    load_benchmark_scenario,
    run_kb_clone_benchmark,
)
from src.backend.utils.runtime_env import load_secret_from_env_or_file


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a benchmark scenario in an isolated KB clone."
    )
    parser.add_argument(
        "--scenario",
        required=True,
        help="Path to benchmark scenario JSON.",
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts/benchmark_runs",
        help="Directory where run bundles are written.",
    )
    parser.add_argument(
        "--source-db-name",
        default=None,
        help="Optional source DB override (otherwise scenario or VON_DB_NAME is used).",
    )
    parser.add_argument(
        "--clone-db-prefix",
        default="benchmark_clone",
        help="Prefix for generated cloned DB names.",
    )
    parser.add_argument(
        "--retain-on-failure",
        action="store_true",
        help="Keep cloned DB when scenario execution fails and archives are verified.",
    )
    parser.add_argument(
        "--allow-non-test-db",
        action="store_true",
        help="Bypass isolated-test-db guard (dangerous; defaults to blocked).",
    )
    parser.add_argument(
        "--mongo-uri",
        default=None,
        help="Optional Mongo URI override.",
    )
    return parser


def _resolve_mongo_uri(cli_value: str | None) -> str:
    if isinstance(cli_value, str) and cli_value.strip():
        return cli_value.strip()
    return (
        load_secret_from_env_or_file("MONGO_URI", "MONGO_URI_FILE")
        or os.getenv("MONGO_URI")
        or "mongodb://localhost:27017/"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    scenario = load_benchmark_scenario(args.scenario)
    app = build_default_benchmark_app()
    mongo_uri = _resolve_mongo_uri(args.mongo_uri)
    client = MongoClient(mongo_uri)
    try:
        result = run_kb_clone_benchmark(
            scenario=scenario,
            output_root=Path(args.output_dir),
            app=app,
            mongo_client=client,
            source_db_name=args.source_db_name,
            clone_db_prefix=args.clone_db_prefix,
            allow_non_test_db=bool(args.allow_non_test_db),
            retain_on_failure=bool(args.retain_on_failure),
        )
    except BenchmarkHarnessError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, indent=2))
        return 1
    finally:
        client.close()

    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if bool(result.get("status", {}).get("complete")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
