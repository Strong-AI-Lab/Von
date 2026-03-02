"""KB-clone benchmark harness for agentic conversational evaluations.

This service executes scripted conversational scenarios against a temporary clone
of a source knowledge-base database. It enforces lifecycle guardrails:

1. Clone source DB into an isolated benchmark DB.
2. Inject a dedicated benchmark user and organisation via Vontology MCP tools.
3. Execute scripted turns through the Flask `/von/generate` API route.
4. Capture per-turn outcomes, timings, and tool traces.
5. Export deterministic compressed pre/post snapshot archives with checksums.
6. Verify archives before teardown, then delete the clone unless retention rules
   require keeping it for debugging.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence

from bson import json_util
from flask import Flask
from pymongo import MongoClient
from pymongo.collection import Collection

from ..integrations.internal_mcp import (
    InternalMCPGateway,
    InternalMCPTransport,
    build_default_catalogue,
)
from ..languagemodels.llm_interface import get_llm_client
from ..server.utils_flask import create_flask_app
from .namespace_service import derive_namespace

_SNAPSHOT_SCHEMA_VERSION = "kb_snapshot_archive.v1"
_METRICS_SCHEMA_VERSION = "kb_clone_benchmark_metrics.v1"
_MANIFEST_SCHEMA_VERSION = "kb_clone_benchmark_manifest.v1"

_DEFAULT_USER_PARENT_CANDIDATES = (
    "#V#von_user",
    "#V#person",
    "#V#human",
    "#V#agent",
)
_DEFAULT_ORG_PARENT_CANDIDATES = (
    "#V#organisation",
    "#V#organization",
    "#V#social_group",
    "#V#agent",
)

_INDEX_OPTION_KEYS = (
    "unique",
    "sparse",
    "expireAfterSeconds",
    "partialFilterExpression",
    "collation",
    "background",
    "weights",
    "default_language",
    "language_override",
    "textIndexVersion",
    "sphereIndexVersion",
    "bits",
    "min",
    "max",
    "bucketSize",
    "wildcardProjection",
    "hidden",
)


class BenchmarkHarnessError(RuntimeError):
    """Raised when benchmark harness preconditions or lifecycle steps fail."""


@dataclass(frozen=True)
class SnapshotArchive:
    label: str
    relative_path: str
    sha256: str
    size_bytes: int
    collection_counts: dict[str, int]
    verified: bool
    verification_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "collection_counts": dict(self.collection_counts),
            "verified": self.verified,
            "verification_error": self.verification_error,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitize_slug(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned.startswith("#v#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.replace("@", "_")
    cleaned = re.sub(r"[^a-z0-9_]+", "_", cleaned).strip("_")
    if not cleaned:
        raise BenchmarkHarnessError(f"Cannot derive slug from value: {value!r}")
    return cleaned


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )


def _ensure_safe_db_name(db_name: str, *, role: str, allow_non_test_db: bool) -> None:
    trimmed = db_name.strip()
    lowered = trimmed.lower()
    if not trimmed:
        raise BenchmarkHarnessError(f"{role} database name is empty.")
    if lowered == "von_db":
        raise BenchmarkHarnessError(
            f"{role} database '{trimmed}' is blocked (production DB name)."
        )
    if allow_non_test_db:
        return
    looks_isolated = (
        lowered.startswith("test_")
        or lowered.startswith("benchmark_")
        or lowered.endswith("_test")
        or lowered.endswith("_benchmark")
    )
    if not looks_isolated:
        raise BenchmarkHarnessError(
            f"{role} database '{trimmed}' is not an isolated test/benchmark DB."
        )


@contextmanager
def _temporary_env(overrides: Mapping[str, str]) -> Iterator[None]:
    prior: dict[str, str | None] = {}
    for key, value in overrides.items():
        prior[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, previous in prior.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _build_default_gateway() -> InternalMCPGateway:
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    gateway.enable()
    return gateway


def _invoke_gateway(
    gateway: InternalMCPGateway, method_name: str, payload: MutableMapping[str, Any]
) -> dict[str, Any]:
    result = gateway.invoke(method_name, payload).payload
    if not isinstance(result, Mapping):
        raise BenchmarkHarnessError(
            f"Gateway method '{method_name}' returned non-mapping payload."
        )
    result_dict = dict(result)
    if result_dict.get("success") is False:
        raise BenchmarkHarnessError(
            f"Gateway method '{method_name}' failed: {result_dict.get('message') or result_dict.get('error') or result_dict}"
        )
    return result_dict


def _resolve_parent_id(
    gateway: InternalMCPGateway, candidates: Iterable[str], *, parent_kind: str
) -> str:
    for candidate in candidates:
        payload = _invoke_gateway(gateway, "concept_exists", {"concept_id": candidate})
        if payload.get("exists") is True:
            return candidate
    raise BenchmarkHarnessError(
        f"No existing parent concept found for {parent_kind}. Candidates={list(candidates)}"
    )


def _extract_concept_id(create_payload: Mapping[str, Any]) -> str:
    results = create_payload.get("results")
    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, Mapping):
                continue
            concept_id = entry.get("concept_id") or entry.get("existing_concept_id")
            if isinstance(concept_id, str) and concept_id.strip():
                return concept_id.strip()
    concept_id = create_payload.get("concept_id")
    if isinstance(concept_id, str) and concept_id.strip():
        return concept_id.strip()
    raise BenchmarkHarnessError(
        f"Missing concept_id in create_concepts output: {create_payload}"
    )


def _create_benchmark_identity(
    *,
    gateway: InternalMCPGateway,
    run_id: str,
    user_name: str | None,
    organisation_name: str | None,
    user_parent_candidates: Iterable[str],
    org_parent_candidates: Iterable[str],
) -> tuple[str, str]:
    user_parent = _resolve_parent_id(
        gateway, user_parent_candidates, parent_kind="benchmark user"
    )
    org_parent = _resolve_parent_id(
        gateway, org_parent_candidates, parent_kind="benchmark organisation"
    )

    resolved_user_name = user_name or f"benchmark_user_{run_id}"
    resolved_org_name = organisation_name or f"benchmark_organisation_{run_id}"

    org_create = _invoke_gateway(
        gateway,
        "create_concepts",
        {
            "parent_id": org_parent,
            "concepts": [{"name": resolved_org_name, "kind": "instance"}],
            "scope_mode": "global_general",
            "allow_duplicate_instances": False,
        },
    )
    org_concept_id = _extract_concept_id(org_create)

    user_create = _invoke_gateway(
        gateway,
        "create_concepts",
        {
            "parent_id": user_parent,
            "concepts": [{"name": resolved_user_name, "kind": "instance"}],
            "scope_mode": "global_general",
            "allow_duplicate_instances": False,
        },
    )
    user_concept_id = _extract_concept_id(user_create)
    return user_concept_id, org_concept_id


def _copy_indexes(source: Collection, target: Collection) -> None:
    for index in source.list_indexes():
        if index.get("name") == "_id_":
            continue
        key_spec = index.get("key")
        if key_spec is None:
            continue
        key_items = list(key_spec.items())
        options: dict[str, Any] = {}
        for option_key in _INDEX_OPTION_KEYS:
            if option_key in index:
                options[option_key] = index[option_key]
        index_name = index.get("name")
        if isinstance(index_name, str) and index_name:
            options["name"] = index_name
        target.create_index(key_items, **options)


def _clone_database(
    client: MongoClient, *, source_db_name: str, clone_db_name: str
) -> dict[str, int]:
    source_db = client[source_db_name]
    if clone_db_name in client.list_database_names():
        client.drop_database(clone_db_name)
    clone_db = client[clone_db_name]

    collection_counts: dict[str, int] = {}
    for collection_name in sorted(source_db.list_collection_names()):
        source_collection = source_db[collection_name]
        clone_collection = clone_db[collection_name]

        docs = [copy.deepcopy(doc) for doc in source_collection.find({})]
        if docs:
            clone_collection.insert_many(docs, ordered=False)
        _copy_indexes(source_collection, clone_collection)
        collection_counts[collection_name] = len(docs)
    return collection_counts


def _iter_collection_json_lines(collection: Collection) -> tuple[bytes, int]:
    buffer = io.StringIO()
    count = 0
    for doc in collection.find({}, sort=[("_id", 1)]):
        buffer.write(json_util.dumps(doc, sort_keys=True))
        buffer.write("\n")
        count += 1
    return buffer.getvalue().encode("utf-8"), count


def _verify_snapshot_archive(
    archive_path: Path, expected_sha256: str
) -> tuple[bool, str | None]:
    actual_sha = _hash_file(archive_path)
    if actual_sha != expected_sha256:
        return False, "sha256_mismatch"

    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getnames()
            if "snapshot_metadata.json" not in members:
                return False, "missing_snapshot_metadata"
            for member in members:
                extracted = tar.extractfile(member)
                if extracted is None:
                    return False, f"unreadable_member:{member}"
                _ = extracted.read(16)
    except Exception as exc:
        return False, f"archive_read_failed:{exc}"

    return True, None


def _build_snapshot_archive(
    *,
    client: MongoClient,
    db_name: str,
    archive_path: Path,
    label: str,
) -> SnapshotArchive:
    db = client[db_name]
    collection_counts: dict[str, int] = {}
    archive_members: dict[str, bytes] = {}

    for collection_name in sorted(db.list_collection_names()):
        payload, count = _iter_collection_json_lines(db[collection_name])
        collection_counts[collection_name] = count
        archive_members[f"{collection_name}.jsonl"] = payload

    metadata = {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "label": label,
        "db_name": db_name,
        "created_at_utc": _utc_now_iso(),
        "collection_counts": collection_counts,
    }
    archive_members["snapshot_metadata.json"] = json.dumps(
        metadata, sort_keys=True, ensure_ascii=True, indent=2
    ).encode("utf-8")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as raw_fh:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_fh,
            mtime=0,
        ) as gzip_fh:
            with tarfile.open(
                fileobj=gzip_fh, mode="w", format=tarfile.PAX_FORMAT
            ) as tar:
                for member_name in sorted(archive_members.keys()):
                    data = archive_members[member_name]
                    info = tarfile.TarInfo(name=member_name)
                    info.size = len(data)
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    tar.addfile(info, io.BytesIO(data))

    sha256 = _hash_file(archive_path)
    verified, verification_error = _verify_snapshot_archive(archive_path, sha256)
    return SnapshotArchive(
        label=label,
        relative_path=archive_path.name,
        sha256=sha256,
        size_bytes=archive_path.stat().st_size,
        collection_counts=collection_counts,
        verified=verified,
        verification_error=verification_error,
    )


def _configure_session_context(
    *,
    api_client: Any,
    user_concept_id: str,
    organisation_concept_id: str,
) -> str:
    user_slug = _sanitize_slug(user_concept_id)
    with api_client.session_transaction() as flask_session:
        flask_session["user_id"] = user_concept_id
        flask_session["user_concept_id"] = user_concept_id
        flask_session["user_email"] = f"{user_slug}@benchmark.local"

    set_user_resp = api_client.post(
        "/von/api/session/set_user_concept",
        json={"user_concept_id": user_concept_id},
    )
    if set_user_resp.status_code != 200:
        raise BenchmarkHarnessError(
            f"set_user_concept failed: status={set_user_resp.status_code} body={set_user_resp.get_data(as_text=True)}"
        )

    set_org_resp = api_client.post(
        "/von/api/session/set_organisation",
        json={"organisation_concept_id": organisation_concept_id},
    )
    if set_org_resp.status_code != 200:
        raise BenchmarkHarnessError(
            f"set_organisation failed: status={set_org_resp.status_code} body={set_org_resp.get_data(as_text=True)}"
        )

    create_session_resp = api_client.post(
        "/von/api/session/create_chat_session",
        json={"session_name": f"Benchmark session {uuid.uuid4().hex[:8]}"},
    )
    if create_session_resp.status_code != 200:
        raise BenchmarkHarnessError(
            f"create_chat_session failed: status={create_session_resp.status_code} body={create_session_resp.get_data(as_text=True)}"
        )

    payload = create_session_resp.get_json(silent=True) or {}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise BenchmarkHarnessError("Missing session_id after create_chat_session.")
    return session_id.strip()


def _evaluate_turn(
    *,
    response_text: str,
    expected_substrings: list[str],
    forbidden_substrings: list[str],
    expected_regex: str | None,
    case_sensitive: bool,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    candidate_text = response_text if case_sensitive else response_text.lower()

    for expected in expected_substrings:
        expected_candidate = expected if case_sensitive else expected.lower()
        if expected_candidate not in candidate_text:
            failures.append(f"missing_expected_substring:{expected}")

    for forbidden in forbidden_substrings:
        forbidden_candidate = forbidden if case_sensitive else forbidden.lower()
        if forbidden_candidate in candidate_text:
            failures.append(f"forbidden_substring_present:{forbidden}")

    if expected_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        if re.search(expected_regex, response_text, flags=flags) is None:
            failures.append(f"regex_not_matched:{expected_regex}")

    return (len(failures) == 0), failures


def _run_turn(
    *,
    api_client: Any,
    turn_spec: Mapping[str, Any],
    run_id: str,
    run_index: int,
    turn_index: int,
    model: str | None,
    retry_budget: int,
    user_concept_id: str,
    organisation_concept_id: str,
) -> dict[str, Any]:
    prompt = str(turn_spec.get("prompt") or "").strip()
    if not prompt:
        raise BenchmarkHarnessError(f"Turn {turn_index} is missing prompt text.")

    expected_substrings = [
        str(item)
        for item in (turn_spec.get("expected_substrings") or [])
        if isinstance(item, str) and item.strip()
    ]
    forbidden_substrings = [
        str(item)
        for item in (turn_spec.get("forbidden_substrings") or [])
        if isinstance(item, str) and item.strip()
    ]
    expected_regex = turn_spec.get("expected_regex")
    expected_regex = (
        expected_regex.strip()
        if isinstance(expected_regex, str) and expected_regex.strip()
        else None
    )
    case_sensitive = bool(turn_spec.get("case_sensitive", False))
    turn_retry_budget = turn_spec.get("retry_budget", retry_budget)
    max_attempts = max(1, int(turn_retry_budget) + 1)

    attempts: list[dict[str, Any]] = []
    final_success = False
    for attempt in range(1, max_attempts + 1):
        request_id = f"{run_id}-r{run_index}-t{turn_index}-a{attempt}"
        payload: dict[str, Any] = {
            "prompt": prompt,
            "client_request_id": request_id,
            "user_id": user_concept_id,
            "org_id": organisation_concept_id,
            "language": "en-NZ",
        }
        if isinstance(model, str) and model.strip():
            payload["model"] = model.strip()

        start_perf = time.perf_counter()
        response = api_client.post("/von/generate", json=payload)
        duration_ms = (time.perf_counter() - start_perf) * 1000.0
        response_payload = response.get_json(silent=True) or {}
        response_text = ""
        if isinstance(response_payload, Mapping):
            raw_response = response_payload.get("response")
            if isinstance(raw_response, str):
                response_text = raw_response

        success = False
        failure_reasons: list[str] = []
        if response.status_code != 200:
            failure_reasons.append(f"http_status:{response.status_code}")
        else:
            success, failure_reasons = _evaluate_turn(
                response_text=response_text,
                expected_substrings=expected_substrings,
                forbidden_substrings=forbidden_substrings,
                expected_regex=expected_regex,
                case_sensitive=case_sensitive,
            )

        llm_debug = (
            response_payload.get("llm_debug")
            if isinstance(response_payload, Mapping)
            else None
        )
        tool_invocations = []
        if isinstance(llm_debug, Mapping):
            invocations = llm_debug.get("tool_invocations")
            if isinstance(invocations, list):
                tool_invocations = invocations

        attempts.append(
            {
                "attempt_index": attempt,
                "request_id": request_id,
                "status_code": int(response.status_code),
                "duration_ms": round(duration_ms, 3),
                "success": bool(success),
                "failure_reasons": failure_reasons,
                "response_preview": response_text[:300],
                "tool_invocations": tool_invocations,
                "llm_debug": llm_debug if isinstance(llm_debug, Mapping) else None,
            }
        )

        if success:
            final_success = True
            break

    return {
        "turn_index": turn_index,
        "prompt": prompt,
        "max_attempts": max_attempts,
        "attempt_count": len(attempts),
        "success": final_success,
        "attempts": attempts,
        "failure_reasons": attempts[-1].get("failure_reasons", []) if attempts else [],
    }


def _run_single_scenario(
    *,
    app: Flask,
    turns: list[Mapping[str, Any]],
    model: str | None,
    run_id: str,
    run_index: int,
    retry_budget: int,
    user_concept_id: str,
    organisation_concept_id: str,
) -> dict[str, Any]:
    with app.test_client() as client:
        session_id = _configure_session_context(
            api_client=client,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
        )
        _ = client.post("/von/reset", json={})

        start_perf = time.perf_counter()
        turn_results: list[dict[str, Any]] = []
        for idx, turn_spec in enumerate(turns, start=1):
            turn_results.append(
                _run_turn(
                    api_client=client,
                    turn_spec=turn_spec,
                    run_id=run_id,
                    run_index=run_index,
                    turn_index=idx,
                    model=model,
                    retry_budget=retry_budget,
                    user_concept_id=user_concept_id,
                    organisation_concept_id=organisation_concept_id,
                )
            )

        duration_ms = (time.perf_counter() - start_perf) * 1000.0
        run_success = all(bool(turn.get("success")) for turn in turn_results)
        return {
            "run_index": run_index,
            "session_id": session_id,
            "duration_ms": round(duration_ms, 3),
            "success": run_success,
            "turns": turn_results,
            "failed_turn_count": sum(
                1 for turn in turn_results if not bool(turn.get("success"))
            ),
        }


def _drop_database(client: MongoClient, db_name: str) -> None:
    client.drop_database(db_name)


def _compute_reliability_metrics(
    run_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    run_count = len(run_results)
    successful_runs = sum(1 for item in run_results if bool(item.get("success")))
    pass_at_k = 1.0 if successful_runs > 0 else 0.0
    completion_consistency = (
        float(successful_runs) / float(run_count) if run_count > 0 else 0.0
    )

    turn_latencies: list[float] = []
    for run in run_results:
        for turn in run.get("turns") or []:
            if not isinstance(turn, Mapping):
                continue
            for attempt in turn.get("attempts") or []:
                if not isinstance(attempt, Mapping):
                    continue
                duration_ms = attempt.get("duration_ms")
                if isinstance(duration_ms, (int, float)):
                    turn_latencies.append(float(duration_ms))
    mean_turn_latency_ms = (
        float(sum(turn_latencies)) / float(len(turn_latencies))
        if turn_latencies
        else 0.0
    )
    return {
        "run_count": run_count,
        "successful_runs": successful_runs,
        "failed_runs": run_count - successful_runs,
        "pass_at_k": round(pass_at_k, 6),
        "completion_consistency": round(completion_consistency, 6),
        "mean_turn_latency_ms": round(mean_turn_latency_ms, 3),
    }


def build_default_benchmark_app() -> Flask:
    """Build a Flask app configured with the default LLM client."""

    llm_client = get_llm_client()
    return create_flask_app(
        list_models_func=llm_client.list_models,
        generate_func=llm_client.generate,
    )


def load_benchmark_scenario(path: str | Path) -> dict[str, Any]:
    scenario_path = Path(path).expanduser().resolve()
    payload = json.loads(scenario_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise BenchmarkHarnessError("Scenario file must contain a JSON object.")
    return payload


def run_kb_clone_benchmark(
    *,
    scenario: Mapping[str, Any],
    output_root: str | Path,
    app: Flask,
    mongo_client: MongoClient,
    gateway: InternalMCPGateway | None = None,
    source_db_name: str | None = None,
    clone_db_prefix: str = "benchmark_clone",
    allow_non_test_db: bool = False,
    retain_on_failure: bool | None = None,
) -> dict[str, Any]:
    """Execute one benchmark scenario end-to-end in an isolated cloned DB."""

    scenario_id = str(scenario.get("scenario_id") or "unnamed_scenario").strip()
    if not scenario_id:
        raise BenchmarkHarnessError("Scenario requires non-empty scenario_id.")

    turns_raw = scenario.get("turns")
    if not isinstance(turns_raw, list) or not turns_raw:
        raise BenchmarkHarnessError("Scenario requires non-empty turns array.")
    turns: list[Mapping[str, Any]] = [
        turn for turn in turns_raw if isinstance(turn, Mapping)
    ]
    if len(turns) != len(turns_raw):
        raise BenchmarkHarnessError("Scenario turns must all be JSON objects.")

    configured_source_db = source_db_name or scenario.get("source_db_name")
    if not isinstance(configured_source_db, str) or not configured_source_db.strip():
        configured_source_db = os.environ.get("VON_DB_NAME") or "test_von_db"
    source_db = configured_source_db.strip()
    _ensure_safe_db_name(source_db, role="source", allow_non_test_db=allow_non_test_db)

    run_count = int(scenario.get("runs") or 1)
    run_count = max(1, run_count)
    mode = str(scenario.get("mode") or ("single" if run_count == 1 else "reliability"))
    retry_budget = int(scenario.get("retry_budget") or 0)
    retry_budget = max(0, retry_budget)
    model = scenario.get("model")
    model_name = model.strip() if isinstance(model, str) and model.strip() else None

    retain_failures = (
        bool(retain_on_failure)
        if retain_on_failure is not None
        else bool(scenario.get("retain_on_failure", False))
    )

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    clone_db_name = f"{clone_db_prefix}_{_sanitize_slug(source_db)}_{run_id}".lower()
    _ensure_safe_db_name(clone_db_name, role="clone", allow_non_test_db=True)

    bundle_root = Path(output_root).expanduser().resolve() / scenario_id / run_id
    archives_dir = bundle_root / "archives"
    metrics_path = bundle_root / "metrics.json"
    manifest_path = bundle_root / "manifest.json"
    bundle_root.mkdir(parents=True, exist_ok=True)
    archives_dir.mkdir(parents=True, exist_ok=True)

    started_at = _utc_now_iso()
    gateway_instance = gateway or _build_default_gateway()

    clone_created = False
    teardown_status: dict[str, Any] = {
        "attempted": False,
        "deleted": False,
        "retained": False,
        "reason": None,
        "error": None,
    }
    pre_snapshot: SnapshotArchive | None = None
    post_snapshot: SnapshotArchive | None = None
    scenario_runs: list[dict[str, Any]] = []
    scenario_success = False
    namespace = None
    benchmark_user = None
    benchmark_org = None
    clone_summary: dict[str, int] = {}
    run_errors: list[str] = []

    try:
        clone_summary = _clone_database(
            mongo_client,
            source_db_name=source_db,
            clone_db_name=clone_db_name,
        )
        clone_created = True

        with _temporary_env({"VON_DB_NAME": clone_db_name}):
            identity_cfg = scenario.get("identity")
            if not isinstance(identity_cfg, Mapping):
                identity_cfg = {}

            user_parent_candidates = identity_cfg.get("user_parent_candidates")
            if not isinstance(user_parent_candidates, list) or not user_parent_candidates:
                user_parent_candidates = list(_DEFAULT_USER_PARENT_CANDIDATES)

            org_parent_candidates = identity_cfg.get("organisation_parent_candidates")
            if not isinstance(org_parent_candidates, list) or not org_parent_candidates:
                org_parent_candidates = list(_DEFAULT_ORG_PARENT_CANDIDATES)

            benchmark_user, benchmark_org = _create_benchmark_identity(
                gateway=gateway_instance,
                run_id=run_id,
                user_name=(
                    identity_cfg.get("user_name")
                    if isinstance(identity_cfg.get("user_name"), str)
                    else None
                ),
                organisation_name=(
                    identity_cfg.get("organisation_name")
                    if isinstance(identity_cfg.get("organisation_name"), str)
                    else None
                ),
                user_parent_candidates=[str(item) for item in user_parent_candidates],
                org_parent_candidates=[str(item) for item in org_parent_candidates],
            )

            namespace = derive_namespace(
                _sanitize_slug(benchmark_user),
                _sanitize_slug(benchmark_org),
            )

            pre_snapshot = _build_snapshot_archive(
                client=mongo_client,
                db_name=clone_db_name,
                archive_path=archives_dir / "pre_run_snapshot.tar.gz",
                label="pre_run",
            )

            for run_index in range(1, run_count + 1):
                scenario_runs.append(
                    _run_single_scenario(
                        app=app,
                        turns=turns,
                        model=model_name,
                        run_id=run_id,
                        run_index=run_index,
                        retry_budget=retry_budget,
                        user_concept_id=benchmark_user,
                        organisation_concept_id=benchmark_org,
                    )
                )

            scenario_success = all(run.get("success") for run in scenario_runs)

            post_snapshot = _build_snapshot_archive(
                client=mongo_client,
                db_name=clone_db_name,
                archive_path=archives_dir / "post_run_snapshot.tar.gz",
                label="post_run",
            )

    except Exception as exc:
        run_errors.append(str(exc))

    archives_verified = bool(
        pre_snapshot
        and post_snapshot
        and pre_snapshot.verified
        and post_snapshot.verified
    )

    if clone_created:
        if not archives_verified:
            teardown_status.update(
                {
                    "attempted": False,
                    "deleted": False,
                    "retained": True,
                    "reason": "archive_verification_failed",
                }
            )
        elif retain_failures and not scenario_success:
            teardown_status.update(
                {
                    "attempted": False,
                    "deleted": False,
                    "retained": True,
                    "reason": "retained_on_failure",
                }
            )
        else:
            teardown_status["attempted"] = True
            try:
                _drop_database(mongo_client, clone_db_name)
                teardown_status.update(
                    {"deleted": True, "retained": False, "reason": "deleted"}
                )
            except Exception as exc:
                teardown_status.update(
                    {
                        "deleted": False,
                        "retained": True,
                        "reason": "teardown_failed",
                        "error": str(exc),
                    }
                )

    metrics = {
        "schema_version": _METRICS_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "run_id": run_id,
        "mode": mode,
        "retry_budget": retry_budget,
        "model": model_name,
        "runs": scenario_runs,
        "aggregate": _compute_reliability_metrics(scenario_runs),
    }
    _write_json(metrics_path, metrics)

    teardown_failed = bool(
        teardown_status.get("attempted") and not teardown_status.get("deleted")
    )
    complete = (
        len(run_errors) == 0
        and pre_snapshot is not None
        and post_snapshot is not None
        and archives_verified
        and not teardown_failed
    )

    manifest = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "run_id": run_id,
        "started_at_utc": started_at,
        "completed_at_utc": _utc_now_iso(),
        "source_db_name": source_db,
        "clone_db_name": clone_db_name,
        "clone_summary": clone_summary,
        "mode": mode,
        "run_count": run_count,
        "model": model_name,
        "namespace": namespace,
        "benchmark_identity": {
            "user_concept_id": benchmark_user,
            "organisation_concept_id": benchmark_org,
        },
        "archive_verification_passed": archives_verified,
        "archives": {
            "pre_run": pre_snapshot.to_dict() if pre_snapshot else None,
            "post_run": post_snapshot.to_dict() if post_snapshot else None,
        },
        "metrics_relative_path": metrics_path.name,
        "teardown": teardown_status,
        "status": {
            "complete": complete,
            "scenario_success": scenario_success,
            "errors": run_errors,
        },
        "reliability_metrics": metrics["aggregate"],
        "bundle_root": str(bundle_root),
        "workflow_manifest": {
            "internal_mcp_orchestrator_status": app.config.get(
                "INTERNAL_MCP_ORCHESTRATOR_STATUS"
            ),
            "internal_mcp_gateway_enabled": bool(
                getattr(app.config.get("INTERNAL_MCP_GATEWAY"), "enabled", False)
            ),
            "requested_model": model_name,
        },
    }
    _write_json(manifest_path, manifest)
    return manifest
