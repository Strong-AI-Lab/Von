"""Read-only Atlas Query Shape Insights diagnostics.

The helpers in this module fetch Atlas Admin API telemetry and emit redacted
operational reports. They are support plumbing only: they do not create indexes,
drop indexes, or author Von workflow/prompt policy.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

from src.backend.utils.runtime_env import (
    clean_env_value,
    get_project_root,
    load_secret_from_env_or_file,
)

_JIRA_KEY = "JVNAUTOSCI-2428"
_RELATED_JIRA_KEY = "JVNAUTOSCI-2427"
_DEFAULT_BASE_URL = "https://cloud.mongodb.com"
_DEFAULT_OAUTH_TOKEN_URL = "https://cloud.mongodb.com/api/oauth/token"
_DEFAULT_ACCEPT_HEADER = "application/vnd.atlas.2025-03-12+json"

_SUMMARY_ENDPOINT = (
    "/api/atlas/v2/groups/{group_id}/clusters/{cluster_name}"
    "/queryShapeInsights/summaries"
)
_QUERY_SHAPES_ENDPOINT = (
    "/api/atlas/v2/groups/{group_id}/clusters/{cluster_name}/queryShapes"
)
_SUGGESTED_INDEXES_ENDPOINT = (
    "/api/atlas/v2/groups/{group_id}/processes/{process_id}"
    "/performanceAdvisor/suggestedIndexes"
)
_SLOW_QUERY_LOGS_ENDPOINT = (
    "/api/atlas/v2/groups/{group_id}/processes/{process_id}"
    "/performanceAdvisor/slowQueryLogs"
)

_DEFAULT_SERIES = (
    "TOTAL_EXECUTION_TIME",
    "AVG_EXECUTION_TIME",
    "EXECUTION_COUNT",
    "DOCS_EXAMINED",
    "KEYS_EXAMINED",
    "DOCS_RETURNED",
    "DOCS_EXAMINED_RETURNED",
    "KEYS_EXAMINED_RETURNED",
    "P90_EXECUTION_TIME",
    "P99_EXECUTION_TIME",
)

_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "total_execution_time_ms": (
        "TOTAL_EXECUTION_TIME",
        "TOTAL_EXECUTION_TIME_MS",
        "TOTAL_EXECUTION_TIME_MILLIS",
        "totalExecutionTime",
        "totalExecutionTimeMs",
        "totalExecutionTimeMillis",
    ),
    "average_execution_time_ms": (
        "AVG_EXECUTION_TIME",
        "AVERAGE_EXECUTION_TIME",
        "AVG_EXECUTION_TIME_MS",
        "averageExecutionTime",
        "averageExecutionTimeMs",
        "avgExecutionTime",
        "avgExecutionTimeMs",
    ),
    "execution_count": (
        "EXECUTION_COUNT",
        "executionCount",
        "count",
        "totalCount",
    ),
    "docs_examined": (
        "DOCS_EXAMINED",
        "DOCUMENTS_EXAMINED",
        "docsExamined",
        "documentsExamined",
    ),
    "keys_examined": (
        "KEYS_EXAMINED",
        "keysExamined",
    ),
    "docs_returned": (
        "DOCS_RETURNED",
        "DOCUMENTS_RETURNED",
        "N_RETURNED",
        "docsReturned",
        "documentsReturned",
        "nReturned",
    ),
    "docs_examined_per_returned": (
        "DOCS_EXAMINED_RETURNED",
        "DOCS_EXAMINED_PER_RETURNED",
        "DOCUMENTS_EXAMINED_RETURNED",
        "docsExaminedReturned",
        "docsExaminedPerReturned",
    ),
    "keys_examined_per_returned": (
        "KEYS_EXAMINED_RETURNED",
        "KEYS_EXAMINED_PER_RETURNED",
        "keysExaminedReturned",
        "keysExaminedPerReturned",
    ),
    "p90_execution_time_ms": (
        "P90_EXECUTION_TIME",
        "P90_EXECUTION_TIME_MS",
        "p90ExecutionTime",
        "p90ExecutionTimeMs",
        "latencyP90Ms",
    ),
    "p99_execution_time_ms": (
        "P99_EXECUTION_TIME",
        "P99_EXECUTION_TIME_MS",
        "p99ExecutionTime",
        "p99ExecutionTimeMs",
        "latencyP99Ms",
    ),
}

_SECRET_ENV_NAMES = {
    "ATLAS_ACCESS_TOKEN",
    "ATLAS_SERVICE_ACCOUNT_CLIENT_ID",
    "ATLAS_SERVICE_ACCOUNT_CLIENT_SECRET",
    "MONGODB_ATLAS_SERVICE_ACCOUNT_CLIENT_ID",
    "MONGODB_ATLAS_SERVICE_ACCOUNT_CLIENT_SECRET",
    "ATLAS_PUBLIC_KEY",
    "ATLAS_PRIVATE_KEY",
    "MONGODB_ATLAS_PUBLIC_KEY",
    "MONGODB_ATLAS_PRIVATE_KEY",
}
_TEXT_REDACTION_LIMIT = 4000


@dataclass(frozen=True)
class AtlasQueryInsightsConfig:
    group_id: str
    cluster_name: str
    base_url: str = _DEFAULT_BASE_URL
    oauth_token_url: str = _DEFAULT_OAUTH_TOKEN_URL
    access_token: str | None = None
    service_account_client_id: str | None = None
    service_account_client_secret: str | None = None
    public_key: str | None = None
    private_key: str | None = None
    accept_header: str = _DEFAULT_ACCEPT_HEADER
    timeout_seconds: float = 30.0

    @property
    def auth_mode(self) -> str:
        if self.access_token:
            return "bearer_token"
        if self.service_account_client_id and self.service_account_client_secret:
            return "service_account"
        if self.public_key and self.private_key:
            return "digest_api_key"
        return "missing_credentials"


@dataclass(frozen=True)
class AtlasQueryInsightsFilters:
    since: str | None = None
    until: str | None = None
    namespaces: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    query_shape_hashes: tuple[str, ...] = ()
    series: tuple[str, ...] = _DEFAULT_SERIES
    max_results: int = 100
    include_query_shapes: bool = False
    include_shape_text: bool = False
    include_suggested_indexes: bool = False
    process_ids: tuple[str, ...] = ()
    include_slow_query_logs: bool = False


@dataclass(frozen=True)
class AtlasTypedBlocker(Exception):
    blocker_type: str
    message: str
    status_code: int | None = None
    endpoint: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_report(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "schema_version": "atlas_query_insights_report.v1",
            "status": "blocked",
            "attribution_jira": _JIRA_KEY,
            "related_jira": _RELATED_JIRA_KEY,
            "blocker": {
                "type": self.blocker_type,
                "message": self.message,
            },
        }
        if self.status_code is not None:
            report["blocker"]["status_code"] = self.status_code
        if self.endpoint:
            report["blocker"]["endpoint"] = self.endpoint
        if self.details:
            report["blocker"]["details"] = dict(self.details)
        return report


def _first_secret(*names: str) -> str | None:
    for name in names:
        value = load_secret_from_env_or_file(name, f"{name}_FILE")
        if value:
            return value
    return None


def _first_env(*names: str) -> str | None:
    for name in names:
        value = clean_env_value(os.getenv(name))
        if value:
            return value
    return None


def load_atlas_query_insights_config_from_env(
    *,
    group_id: str | None = None,
    cluster_name: str | None = None,
) -> AtlasQueryInsightsConfig:
    """Resolve Atlas Admin API configuration without printing secrets."""

    resolved_group_id = clean_env_value(group_id) or _first_env(
        "ATLAS_GROUP_ID",
        "MONGODB_ATLAS_GROUP_ID",
        "ATLAS_PROJECT_ID",
        "MONGODB_ATLAS_PROJECT_ID",
    )
    resolved_cluster = clean_env_value(cluster_name) or _first_env(
        "ATLAS_CLUSTER_NAME",
        "MONGODB_ATLAS_CLUSTER_NAME",
    )
    if not resolved_group_id:
        raise AtlasTypedBlocker(
            "missing_project_id",
            "Atlas project/group id is required via --group-id or ATLAS_GROUP_ID.",
        )
    if not resolved_cluster:
        raise AtlasTypedBlocker(
            "missing_cluster_name",
            "Atlas cluster name is required via --cluster-name or ATLAS_CLUSTER_NAME.",
        )

    return AtlasQueryInsightsConfig(
        group_id=resolved_group_id,
        cluster_name=resolved_cluster,
        base_url=_first_env("ATLAS_ADMIN_BASE_URL", "MONGODB_ATLAS_ADMIN_BASE_URL")
        or _DEFAULT_BASE_URL,
        oauth_token_url=_first_env("ATLAS_OAUTH_TOKEN_URL")
        or _DEFAULT_OAUTH_TOKEN_URL,
        access_token=_first_secret("ATLAS_ACCESS_TOKEN", "MONGODB_ATLAS_ACCESS_TOKEN"),
        service_account_client_id=_first_secret(
            "ATLAS_SERVICE_ACCOUNT_CLIENT_ID",
            "MONGODB_ATLAS_SERVICE_ACCOUNT_CLIENT_ID",
        ),
        service_account_client_secret=_first_secret(
            "ATLAS_SERVICE_ACCOUNT_CLIENT_SECRET",
            "MONGODB_ATLAS_SERVICE_ACCOUNT_CLIENT_SECRET",
        ),
        public_key=_first_secret("ATLAS_PUBLIC_KEY", "MONGODB_ATLAS_PUBLIC_KEY"),
        private_key=_first_secret("ATLAS_PRIVATE_KEY", "MONGODB_ATLAS_PRIVATE_KEY"),
        accept_header=_first_env("ATLAS_ADMIN_ACCEPT_HEADER")
        or _DEFAULT_ACCEPT_HEADER,
    )


def _normalised_base_url(base_url: str) -> str:
    return str(base_url or _DEFAULT_BASE_URL).rstrip("/")


def _encode_path(value: str) -> str:
    return quote(str(value), safe="")


def _endpoint(path_template: str, **values: str) -> str:
    encoded = {name: _encode_path(value) for name, value in values.items()}
    return path_template.format(**encoded)


def _headers(config: AtlasQueryInsightsConfig, token: str | None) -> dict[str, str]:
    headers = {
        "Accept": config.accept_header,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class AtlasAdminApiClient:
    def __init__(
        self,
        config: AtlasQueryInsightsConfig,
        *,
        session: Any | None = None,
    ) -> None:
        self.config = config
        self.session = session if session is not None else requests.Session()
        self._token: str | None = config.access_token

    def _ensure_auth(self) -> tuple[dict[str, str], Any | None]:
        mode = self.config.auth_mode
        if mode == "missing_credentials":
            raise AtlasTypedBlocker(
                "missing_credentials",
                (
                    "Atlas Admin API credentials are required via access token, "
                    "service-account credentials, or public/private API key env vars."
                ),
            )
        if mode == "service_account":
            if not self._token:
                self._token = self._fetch_service_account_token()
            return _headers(self.config, self._token), None
        if mode == "bearer_token":
            return _headers(self.config, self.config.access_token), None
        return _headers(self.config, None), HTTPDigestAuth(
            self.config.public_key or "",
            self.config.private_key or "",
        )

    def _fetch_service_account_token(self) -> str:
        response = self.session.post(
            self.config.oauth_token_url,
            data={"grant_type": "client_credentials"},
            auth=(
                self.config.service_account_client_id,
                self.config.service_account_client_secret,
            ),
            headers={"Accept": "application/json"},
            timeout=self.config.timeout_seconds,
        )
        if getattr(response, "status_code", 0) in {401, 403}:
            raise AtlasTypedBlocker(
                "atlas_auth_failed",
                "Atlas service-account token request was rejected.",
                status_code=int(response.status_code),
                endpoint="oauth_token",
            )
        if not bool(getattr(response, "ok", False)):
            raise AtlasTypedBlocker(
                "atlas_request_failed",
                "Atlas service-account token request failed.",
                status_code=int(getattr(response, "status_code", 0) or 0),
                endpoint="oauth_token",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AtlasTypedBlocker(
                "atlas_response_parse_failed",
                "Atlas token response was not valid JSON.",
                endpoint="oauth_token",
            ) from exc
        token = clean_env_value(str(payload.get("access_token") or ""))
        if not token:
            raise AtlasTypedBlocker(
                "atlas_response_parse_failed",
                "Atlas token response did not include an access_token.",
                endpoint="oauth_token",
            )
        return token

    def get_json(
        self,
        path: str,
        *,
        params: Sequence[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        headers, auth = self._ensure_auth()
        url = _normalised_base_url(self.config.base_url) + path
        response = self.session.get(
            url,
            headers=headers,
            params=list(params or ()),
            auth=auth,
            timeout=self.config.timeout_seconds,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in {401, 403}:
            raise AtlasTypedBlocker(
                "insufficient_atlas_role",
                "Atlas Admin API request was rejected; Project Read Only or stronger role may be required.",
                status_code=status_code,
                endpoint=path,
            )
        if status_code in {404, 410}:
            raise AtlasTypedBlocker(
                "atlas_query_insights_unavailable",
                "Atlas Query Shape Insights endpoint is unavailable for this project, cluster, or tier.",
                status_code=status_code,
                endpoint=path,
            )
        if not bool(getattr(response, "ok", False)):
            raise AtlasTypedBlocker(
                "atlas_request_failed",
                "Atlas Admin API request failed.",
                status_code=status_code,
                endpoint=path,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AtlasTypedBlocker(
                "atlas_response_parse_failed",
                "Atlas Admin API response was not valid JSON.",
                status_code=status_code,
                endpoint=path,
            ) from exc
        if not isinstance(payload, Mapping):
            raise AtlasTypedBlocker(
                "atlas_response_parse_failed",
                "Atlas Admin API response was not a JSON object.",
                status_code=status_code,
                endpoint=path,
            )
        return dict(payload)


def _multi_params(name: str, values: Iterable[str]) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for value in values:
        text = clean_env_value(str(value))
        if text:
            params.append((name, text))
    return params


def _atlas_epoch_millis_param(value: str | None) -> str | None:
    text = clean_env_value(value)
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return text
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1000))


def build_query_insights_summary_params(
    filters: AtlasQueryInsightsFilters,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [
        ("nSummaries", str(max(1, min(int(filters.max_results or 100), 100)))),
    ]
    since = _atlas_epoch_millis_param(filters.since)
    until = _atlas_epoch_millis_param(filters.until)
    if since:
        params.append(("since", since))
    if until:
        params.append(("until", until))
    params.extend(_multi_params("namespaces", filters.namespaces))
    params.extend(_multi_params("commands", filters.commands))
    params.extend(_multi_params("queryShapeHashes", filters.query_shape_hashes))
    params.extend(_multi_params("series", filters.series or _DEFAULT_SERIES))
    return params


def build_query_shapes_params(
    filters: AtlasQueryInsightsFilters,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    since = _atlas_epoch_millis_param(filters.since)
    until = _atlas_epoch_millis_param(filters.until)
    if since:
        params.append(("since", since))
    if until:
        params.append(("until", until))
    params.extend(_multi_params("namespaces", filters.namespaces))
    params.extend(_multi_params("commands", filters.commands))
    params.extend(_multi_params("queryShapeHashes", filters.query_shape_hashes))
    params.append(("nExamples", "1" if filters.include_shape_text else "0"))
    return params


def _items_from_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in (
        "results",
        "summaries",
        "queryShapeInsights",
        "queryShapes",
        "suggestedIndexes",
        "items",
        "documents",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    if all(key in payload for key in ("queryShapeHash", "namespace")):
        return [payload]
    return []


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    top = _safe_float(numerator)
    bottom = _safe_float(denominator)
    if top is None:
        return None
    if bottom is None or bottom <= 0:
        bottom = 1.0
    return round(top / bottom, 3)


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _lookup_metric_direct(raw: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    candidate_paths: list[str] = []
    for alias in aliases:
        candidate_paths.extend(
            [
                alias,
                f"metrics.{alias}",
                f"stats.{alias}",
                f"summary.{alias}",
                f"latest.{alias}",
            ]
        )
    for path in candidate_paths:
        value = _get_path(raw, path)
        if value is not None:
            return value
    return None


def _series_metric_name(item: Mapping[str, Any]) -> str:
    for key in ("name", "series", "seriesName", "metricName", "type"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _series_metric_value(item: Mapping[str, Any]) -> Any:
    for key in ("value", "total", "sum", "avg", "average", "p90", "p99", "latest"):
        if key in item:
            return item.get(key)
    data = item.get("data") or item.get("points") or item.get("values")
    if isinstance(data, list) and data:
        last = data[-1]
        if isinstance(last, Mapping):
            for key in ("value", "total", "sum", "avg", "average"):
                if key in last:
                    return last.get(key)
        return last
    return None


def _lookup_metric_from_series(raw: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    alias_set = {alias.lower() for alias in aliases}
    for container_key in ("series", "metrics", "dataSeries", "measurements"):
        container = raw.get(container_key)
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, Mapping):
                continue
            if _series_metric_name(item).lower() in alias_set:
                return _series_metric_value(item)
    return None


def _normalise_metric(raw: Mapping[str, Any], canonical: str) -> float | int | None:
    aliases = _METRIC_ALIASES[canonical]
    value = _lookup_metric_direct(raw, aliases)
    if value is None:
        value = _lookup_metric_from_series(raw, aliases)
    if canonical == "execution_count":
        return _safe_int(value)
    return _safe_float(value)


def _query_hash(raw: Mapping[str, Any]) -> str:
    for key in ("queryShapeHash", "queryHash", "shapeHash", "query_shape_hash", "id"):
        value = clean_env_value(str(raw.get(key) or ""))
        if value:
            return value[:128]
    return ""


def _namespace(raw: Mapping[str, Any]) -> str:
    namespace = clean_env_value(
        str(
            raw.get("namespace")
            or raw.get("ns")
            or _get_path(raw, "queryShape.namespace")
            or ""
        )
    )
    if namespace:
        return namespace[:256]
    database = clean_env_value(str(raw.get("database") or raw.get("db") or ""))
    collection = clean_env_value(
        str(raw.get("collection") or raw.get("coll") or raw.get("collectionName") or "")
    )
    return ".".join(part for part in (database, collection) if part)[:256]


def _command_name(raw: Mapping[str, Any]) -> str:
    for key in ("command", "commandName", "operation", "op"):
        value = clean_env_value(str(raw.get(key) or ""))
        if value:
            return value[:64]
    query_shape = raw.get("queryShape")
    if isinstance(query_shape, Mapping):
        for key in ("command", "commandName"):
            value = clean_env_value(str(query_shape.get(key) or ""))
            if value:
                return value[:64]
    return ""


def _convert_time_to_ms(raw_value: float | int | None) -> float | None:
    if raw_value is None:
        return None
    return round(float(raw_value), 3)


def parse_query_insights_summaries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse Atlas Query Shape Insights summaries into a stable report schema."""

    rows: list[dict[str, Any]] = []
    for raw in _items_from_payload(payload):
        row: dict[str, Any] = {
            "query_shape_hash": _query_hash(raw),
            "namespace": _namespace(raw),
            "command_name": _command_name(raw),
        }
        shape_text = raw.get("queryShape")
        if shape_text is not None:
            row["shape_text_available"] = True
            row["query_shape_text_redacted"] = redact_query_shape_text(shape_text)
            row["shape_text_redaction"] = "literal_values_redacted"
        for canonical in _METRIC_ALIASES:
            value = _normalise_metric(raw, canonical)
            if canonical.endswith("_time_ms"):
                value = _convert_time_to_ms(value)
            row[canonical] = value
        if row.get("docs_examined_per_returned") is None:
            row["docs_examined_per_returned"] = _ratio(
                row.get("docs_examined"), row.get("docs_returned")
            )
        if row.get("keys_examined_per_returned") is None:
            row["keys_examined_per_returned"] = _ratio(
                row.get("keys_examined"), row.get("docs_returned")
            )
        if not row["average_execution_time_ms"]:
            row["average_execution_time_ms"] = _ratio(
                row.get("total_execution_time_ms"), row.get("execution_count")
            )
        row["impact_score"] = _impact_score(row)
        rows.append(row)
    return rows


def _impact_score(row: Mapping[str, Any]) -> float:
    execution_count = float(row.get("execution_count") or 0.0)
    total_time = float(row.get("total_execution_time_ms") or 0.0)
    p99 = float(row.get("p99_execution_time_ms") or 0.0)
    p90 = float(row.get("p90_execution_time_ms") or 0.0)
    docs_ratio = float(row.get("docs_examined_per_returned") or 0.0)
    keys_ratio = float(row.get("keys_examined_per_returned") or 0.0)
    ratio_score = max(docs_ratio, keys_ratio) * max(execution_count, 1.0)
    latency_score = max(p99, p90)
    return round(total_time + ratio_score + latency_score, 3)


def rank_query_insights_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    ranked = [dict(row) for row in rows]
    for row in ranked:
        row["impact_score"] = _impact_score(row)
    ranked.sort(
        key=lambda row: (
            float(row.get("total_execution_time_ms") or 0.0),
            float(row.get("docs_examined_per_returned") or 0.0),
            float(row.get("keys_examined_per_returned") or 0.0),
            float(row.get("p99_execution_time_ms") or 0.0),
            float(row.get("p90_execution_time_ms") or 0.0),
            int(row.get("execution_count") or 0),
        ),
        reverse=True,
    )
    return ranked[: max(1, int(limit or 1))]


def _redact_literal_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _redact_literal_values(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_literal_values(child) for child in value]
    if isinstance(value, tuple):
        return [_redact_literal_values(child) for child in value]
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return "<number>"
    return "<redacted>"


def redact_query_shape_text(value: Any) -> str:
    """Return shape text with literals removed and structural field names retained."""

    if value is None:
        return ""
    if not isinstance(value, str):
        return json.dumps(_redact_literal_values(value), sort_keys=True)[
            :_TEXT_REDACTION_LIMIT
        ]

    text = value
    text = text[:_TEXT_REDACTION_LIMIT]
    text = re.sub(r'ObjectId\("[^"]*"\)', 'ObjectId("<redacted>")', text)
    text = re.sub(
        r'ISODate\("[^"]*"\)',
        'ISODate("<redacted>")',
        text,
    )
    text = re.sub(r'(:\s*)"([^"\\]|\\.)*"', r'\1"<redacted>"', text)
    text = re.sub(r"(:\s*)'([^'\\]|\\.)*'", r"\1'<redacted>'", text)
    text = re.sub(r"(:\s*)-?\d+(?:\.\d+)?\b", r"\1<number>", text)
    return text


def parse_query_shapes(payload: Mapping[str, Any], *, include_shape_text: bool) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for raw in _items_from_payload(payload):
        query_hash = _query_hash(raw)
        if not query_hash:
            continue
        detail: dict[str, Any] = {
            "query_shape_hash": query_hash,
            "namespace": _namespace(raw),
            "command_name": _command_name(raw),
            "shape_text_available": False,
        }
        shape_text = (
            raw.get("queryShape")
            or raw.get("queryShapeText")
            or raw.get("shape")
            or raw.get("example")
        )
        if shape_text is not None:
            detail["shape_text_available"] = True
            if include_shape_text:
                detail["query_shape_text_redacted"] = redact_query_shape_text(shape_text)
                detail["shape_text_redaction"] = "literal_values_redacted"
        details[query_hash] = detail
    return details


def _index_namespace(raw: Mapping[str, Any]) -> str:
    namespace = clean_env_value(str(raw.get("namespace") or raw.get("ns") or ""))
    if namespace:
        return namespace
    database = clean_env_value(str(raw.get("database") or raw.get("db") or ""))
    collection = clean_env_value(
        str(raw.get("collection") or raw.get("collectionName") or raw.get("coll") or "")
    )
    return ".".join(part for part in (database, collection) if part)


def parse_suggested_indexes(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    by_namespace: dict[str, dict[str, Any]] = {}
    for raw in _items_from_payload(payload):
        namespace = _index_namespace(raw)
        if not namespace:
            continue
        entry = by_namespace.setdefault(
            namespace,
            {"namespace": namespace, "suggested_index_count": 0, "index_names": []},
        )
        entry["suggested_index_count"] += 1
        index_name = clean_env_value(str(raw.get("indexName") or raw.get("name") or ""))
        if index_name and index_name not in entry["index_names"]:
            entry["index_names"].append(index_name[:160])
    return by_namespace


def collect_repo_index_evidence(
    *,
    namespaces: Iterable[str],
    repo_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Collect lightweight repo-owned index evidence for matching collections.

    This is deliberately evidence-only. It does not claim full index coverage
    because Python cannot authoritatively decide DB tuning policy here.
    """

    root = repo_root or get_project_root()
    collections = {
        namespace.rsplit(".", 1)[-1]
        for namespace in namespaces
        if isinstance(namespace, str) and "." in namespace
    }
    evidence = {
        namespace: {
            "repo_index_comparison": "not_evaluated",
            "repo_index_evidence_files": [],
        }
        for namespace in namespaces
        if namespace
    }
    if not collections:
        return evidence
    search_roots = [root / "src", root / "scripts"]
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for path in search_root.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "create_index" not in text and "create_indexes" not in text:
                continue
            for namespace in list(evidence):
                collection = namespace.rsplit(".", 1)[-1]
                if collection in text:
                    evidence[namespace]["repo_index_comparison"] = (
                        "candidate_collection_has_repo_index_definitions"
                    )
                    files = evidence[namespace]["repo_index_evidence_files"]
                    rel_path = str(path.relative_to(root))
                    if rel_path not in files:
                        files.append(rel_path)
    return evidence


def _merge_details(
    rows: list[dict[str, Any]],
    *,
    query_shapes: Mapping[str, Mapping[str, Any]],
    suggested_indexes: Mapping[str, Mapping[str, Any]],
    repo_index_evidence: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for row in rows:
        next_row = dict(row)
        query_hash = str(next_row.get("query_shape_hash") or "")
        namespace = str(next_row.get("namespace") or "")
        if query_hash and query_hash in query_shapes:
            for key, value in query_shapes[query_hash].items():
                if value not in (None, "", []):
                    next_row.setdefault(key, value)
        if namespace in suggested_indexes:
            next_row["atlas_suggested_indexes"] = suggested_indexes[namespace]
        if namespace in repo_index_evidence:
            next_row.update(repo_index_evidence[namespace])
        merged.append(next_row)
    return merged


def _redacted_runtime_config(config: AtlasQueryInsightsConfig) -> dict[str, Any]:
    return {
        "group_id_present": bool(config.group_id),
        "cluster_name": config.cluster_name,
        "base_url": config.base_url,
        "auth_mode": config.auth_mode,
        "credential_values_redacted": True,
    }


def build_atlas_query_insights_report(
    config: AtlasQueryInsightsConfig,
    filters: AtlasQueryInsightsFilters,
    *,
    client: AtlasAdminApiClient | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    api = client or AtlasAdminApiClient(config)
    summary_path = _endpoint(
        _SUMMARY_ENDPOINT,
        group_id=config.group_id,
        cluster_name=config.cluster_name,
    )
    summary_payload = api.get_json(
        summary_path,
        params=build_query_insights_summary_params(filters),
    )
    parsed_rows = parse_query_insights_summaries(summary_payload)

    query_shapes: dict[str, dict[str, Any]] = {}
    if filters.include_query_shapes and parsed_rows:
        shape_path = _endpoint(
            _QUERY_SHAPES_ENDPOINT,
            group_id=config.group_id,
            cluster_name=config.cluster_name,
        )
        shape_payload = api.get_json(
            shape_path,
            params=build_query_shapes_params(filters),
        )
        query_shapes = parse_query_shapes(
            shape_payload,
            include_shape_text=filters.include_shape_text,
        )

    suggested_indexes: dict[str, dict[str, Any]] = {}
    suggested_index_errors: list[dict[str, Any]] = []
    if filters.include_suggested_indexes:
        for process_id in filters.process_ids:
            process_text = clean_env_value(str(process_id))
            if not process_text:
                continue
            try:
                payload = api.get_json(
                    _endpoint(
                        _SUGGESTED_INDEXES_ENDPOINT,
                        group_id=config.group_id,
                        process_id=process_text,
                    )
                )
            except AtlasTypedBlocker as exc:
                suggested_index_errors.append(exc.to_report()["blocker"])
                continue
            for namespace, value in parse_suggested_indexes(payload).items():
                existing = suggested_indexes.setdefault(
                    namespace,
                    {
                        "namespace": namespace,
                        "suggested_index_count": 0,
                        "index_names": [],
                    },
                )
                existing["suggested_index_count"] += int(
                    value.get("suggested_index_count") or 0
                )
                for index_name in value.get("index_names") or []:
                    if index_name not in existing["index_names"]:
                        existing["index_names"].append(index_name)

    slow_query_log_status = "not_requested"
    if filters.include_slow_query_logs:
        slow_query_log_status = "endpoint_supported_but_not_included_in_report"

    ranked = rank_query_insights_rows(parsed_rows, limit=filters.max_results)
    repo_index_evidence = collect_repo_index_evidence(
        namespaces=[str(row.get("namespace") or "") for row in ranked],
        repo_root=repo_root,
    )
    rows = _merge_details(
        ranked,
        query_shapes=query_shapes,
        suggested_indexes=suggested_indexes,
        repo_index_evidence=repo_index_evidence,
    )
    return {
        "schema_version": "atlas_query_insights_report.v1",
        "status": "ok",
        "attribution_jira": _JIRA_KEY,
        "related_jira": _RELATED_JIRA_KEY,
        "source": "atlas_admin_api",
        "runtime_config": _redacted_runtime_config(config),
        "filters": {
            "since": filters.since,
            "until": filters.until,
            "namespaces": list(filters.namespaces),
            "commands": list(filters.commands),
            "query_shape_hashes": list(filters.query_shape_hashes),
            "series": list(filters.series),
            "max_results": filters.max_results,
            "include_query_shapes": filters.include_query_shapes,
            "include_shape_text": filters.include_shape_text,
            "include_suggested_indexes": filters.include_suggested_indexes,
            "process_ids": list(filters.process_ids),
        },
        "ranking": (
            "total execution time, scanned/returned ratios, P99/P90 latency, "
            "execution count"
        ),
        "observed_shape_count": len(parsed_rows),
        "rows": rows,
        "optional_sources": {
            "query_shapes": "included" if query_shapes else "not_included",
            "suggested_indexes": (
                "included"
                if suggested_indexes
                else "requested_no_rows"
                if filters.include_suggested_indexes
                else "not_requested"
            ),
            "slow_query_logs": slow_query_log_status,
            "suggested_index_errors": suggested_index_errors,
        },
    }


def _contains_secret(rendered: str) -> bool:
    for env_name in _SECRET_ENV_NAMES:
        secret = load_secret_from_env_or_file(env_name, f"{env_name}_FILE")
        if secret and secret in rendered:
            return True
    return False


def assert_report_has_no_known_secrets(report: Mapping[str, Any]) -> None:
    rendered = json.dumps(report, sort_keys=True, default=str)
    if _contains_secret(rendered):
        raise RuntimeError("Atlas diagnostics report contains a configured secret")


def format_atlas_query_insights_table(report: Mapping[str, Any]) -> str:
    if report.get("status") == "blocked":
        blocker = report.get("blocker") if isinstance(report.get("blocker"), Mapping) else {}
        return "\t".join(("status", "blocker_type", "message")) + "\n" + "\t".join(
            (
                "blocked",
                str(blocker.get("type") or ""),
                str(blocker.get("message") or ""),
            )
        )

    rows = report.get("rows")
    if not isinstance(rows, list):
        return ""
    headers = (
        "rank",
        "namespace",
        "cmd",
        "exec_count",
        "total_ms",
        "avg_ms",
        "docs/ret",
        "keys/ret",
        "p90_ms",
        "p99_ms",
        "shape_hash",
        "repo_index",
    )
    lines = ["\t".join(headers)]
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, Mapping):
            continue
        values = (
            index,
            raw.get("namespace") or "",
            raw.get("command_name") or "",
            raw.get("execution_count") or "",
            raw.get("total_execution_time_ms") or "",
            raw.get("average_execution_time_ms") or "",
            raw.get("docs_examined_per_returned") or "",
            raw.get("keys_examined_per_returned") or "",
            raw.get("p90_execution_time_ms") or "",
            raw.get("p99_execution_time_ms") or "",
            raw.get("query_shape_hash") or "",
            raw.get("repo_index_comparison") or "",
        )
        lines.append("\t".join(str(value) for value in values))
    return "\n".join(lines)


__all__ = [
    "AtlasAdminApiClient",
    "AtlasQueryInsightsConfig",
    "AtlasQueryInsightsFilters",
    "AtlasTypedBlocker",
    "assert_report_has_no_known_secrets",
    "build_atlas_query_insights_report",
    "build_query_insights_summary_params",
    "build_query_shapes_params",
    "collect_repo_index_evidence",
    "format_atlas_query_insights_table",
    "load_atlas_query_insights_config_from_env",
    "parse_query_insights_summaries",
    "parse_query_shapes",
    "parse_suggested_indexes",
    "rank_query_insights_rows",
    "redact_query_shape_text",
]
