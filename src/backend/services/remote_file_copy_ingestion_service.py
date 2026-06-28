from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import mimetypes
import os
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import requests

from .computer_file_copy_service import import_bytes_file_copy

_DEFAULT_MAX_BYTES = 25 * 1024 * 1024
_DEFAULT_MAX_REDIRECTS = 5
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
_DEFAULT_READ_TIMEOUT_SECONDS = 30
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 40
_DEFAULT_REGISTRATION_TIMEOUT_SECONDS = 120
_DEFAULT_USER_AGENT = "VonRemoteFileCopyIngestion/1.0"
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_CAPTURED_RESPONSE_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "Content-Length",
    "Content-Disposition",
    "ETag",
    "Last-Modified",
)


def _coerce_configured_int(
    raw_value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(raw_value)
    except Exception:
        return default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _configured_max_bytes(override: int | None = None) -> int:
    if isinstance(override, int) and override > 0:
        return max(1, override)
    return _coerce_configured_int(
        os.getenv("VON_REMOTE_FILE_COPY_MAX_BYTES"),
        default=_DEFAULT_MAX_BYTES,
        minimum=1024,
        maximum=250 * 1024 * 1024,
    )


def _configured_max_redirects(override: int | None = None) -> int:
    if isinstance(override, int) and override >= 0:
        return override
    return _coerce_configured_int(
        os.getenv("VON_REMOTE_FILE_COPY_MAX_REDIRECTS"),
        default=_DEFAULT_MAX_REDIRECTS,
        minimum=0,
        maximum=20,
    )


def _configured_timeouts() -> tuple[int, int]:
    connect_timeout = _coerce_configured_int(
        os.getenv("VON_REMOTE_FILE_COPY_CONNECT_TIMEOUT_SECONDS"),
        default=_DEFAULT_CONNECT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=120,
    )
    read_timeout = _coerce_configured_int(
        os.getenv("VON_REMOTE_FILE_COPY_READ_TIMEOUT_SECONDS"),
        default=_DEFAULT_READ_TIMEOUT_SECONDS,
        minimum=1,
        maximum=300,
    )
    return connect_timeout, read_timeout


def _configured_total_timeout_seconds(override: float | int | None = None) -> float:
    if isinstance(override, (int, float)) and not isinstance(override, bool):
        return max(1.0, min(float(override), 600.0))
    return float(
        _coerce_configured_int(
            os.getenv("VON_REMOTE_FILE_COPY_TOTAL_TIMEOUT_SECONDS"),
            default=_DEFAULT_TOTAL_TIMEOUT_SECONDS,
            minimum=1,
            maximum=600,
        )
    )


def _configured_registration_timeout_seconds(
    override: float | int | None = None,
) -> float:
    if isinstance(override, (int, float)) and not isinstance(override, bool):
        return max(0.001, min(float(override), 600.0))
    return float(
        _coerce_configured_int(
            os.getenv("VON_REMOTE_FILE_COPY_REGISTRATION_TIMEOUT_SECONDS"),
            default=_DEFAULT_REGISTRATION_TIMEOUT_SECONDS,
            minimum=1,
            maximum=600,
        )
    )


def _safe_filename(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("\"'")
    if not cleaned:
        return None
    cleaned = cleaned.replace("\\", "/").split("/")[-1].strip()
    return cleaned or None


def _normalise_content_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned.split(";", 1)[0].strip().lower() or None


def _extract_filename_from_content_disposition(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None

    parts = [segment.strip() for segment in value.split(";") if segment.strip()]
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, sep, raw_val = part.partition("=")
        if not sep:
            continue
        params[key.strip().lower()] = raw_val.strip()

    encoded = params.get("filename*")
    if isinstance(encoded, str) and encoded:
        candidate = encoded.strip().strip("\"'")
        if "''" in candidate:
            _charset, _lang, encoded_name = candidate.partition("''")
            candidate = encoded_name
        return _safe_filename(unquote(candidate))

    plain = params.get("filename")
    if isinstance(plain, str) and plain:
        return _safe_filename(plain)
    return None


def _derive_filename_from_url(url: str) -> str | None:
    parsed = urlsplit(url)
    candidate = Path(unquote(parsed.path or "")).name
    return _safe_filename(candidate)


def _build_fallback_filename(content_type: str | None) -> str:
    extension = mimetypes.guess_extension(content_type or "") if content_type else None
    if extension == ".jpe":
        extension = ".jpg"
    if not isinstance(extension, str) or not extension:
        extension = ".bin"
    return f"downloaded_file{extension}"


def _choose_filename(
    *,
    requested_filename: str | None,
    content_disposition: str | None,
    final_url: str,
    content_type: str | None,
) -> tuple[str, str]:
    explicit_filename = _safe_filename(requested_filename)
    if explicit_filename:
        return explicit_filename, "request.filename"

    disposition_filename = _extract_filename_from_content_disposition(
        content_disposition
    )
    if disposition_filename:
        return disposition_filename, "response.content_disposition"

    url_filename = _derive_filename_from_url(final_url)
    if url_filename:
        return url_filename, "response.final_url"

    return _build_fallback_filename(content_type), "generated.fallback"


def _choose_content_type(
    *,
    response_content_type: str | None,
    original_filename: str,
) -> tuple[str | None, str]:
    guessed_content_type, _encoding = mimetypes.guess_type(original_filename)
    guessed_content_type = _normalise_content_type(guessed_content_type)
    response_content_type = _normalise_content_type(response_content_type)

    if response_content_type and response_content_type not in {
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return response_content_type, "response.content_type"
    if guessed_content_type:
        return guessed_content_type, "filename.extension"
    if response_content_type:
        return response_content_type, "response.content_type_generic"
    return None, "unknown"


def _classify_ip_address(value: str) -> tuple[bool, str]:
    ip = ipaddress.ip_address(value)
    if ip.is_loopback:
        return False, "loopback"
    if ip.is_private:
        return False, "private"
    if ip.is_link_local:
        return False, "link_local"
    if ip.is_multicast:
        return False, "multicast"
    if ip.is_reserved:
        return False, "reserved"
    if ip.is_unspecified:
        return False, "unspecified"
    return True, "public"


def _validate_remote_target(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "").strip().lower()
    if scheme not in {"http", "https"}:
        return {
            "success": False,
            "error": "unsupported_url_scheme",
            "message": "Remote artefact ingestion only supports http and https URLs.",
            "url": url,
            "scheme": scheme or None,
        }
    if parsed.username or parsed.password:
        return {
            "success": False,
            "error": "url_credentials_not_allowed",
            "message": "Remote artefact ingestion does not allow embedded URL credentials.",
            "url": url,
        }

    hostname = parsed.hostname
    if not isinstance(hostname, str) or not hostname.strip():
        return {
            "success": False,
            "error": "missing_remote_host",
            "message": "Remote artefact ingestion requires a URL host.",
            "url": url,
        }

    hostname = hostname.strip().lower().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"}:
        return {
            "success": False,
            "error": "blocked_private_host",
            "message": "Localhost targets are not allowed for remote artefact ingestion.",
            "url": url,
            "host": hostname,
        }

    try:
        addrinfo = socket.getaddrinfo(
            hostname,
            parsed.port or (443 if scheme == "https" else 80),
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        return {
            "success": False,
            "error": "host_resolution_failed",
            "message": f"Could not resolve remote host '{hostname}': {exc}",
            "url": url,
            "host": hostname,
        }

    resolved_addresses: list[str] = []
    blocked_addresses: list[dict[str, str]] = []
    for _family, _socktype, _proto, _canonname, sockaddr in addrinfo:
        if not isinstance(sockaddr, tuple) or not sockaddr:
            continue
        address = str(sockaddr[0]).strip()
        if not address or address in resolved_addresses:
            continue
        resolved_addresses.append(address)
        try:
            allowed, reason = _classify_ip_address(address)
        except ValueError:
            blocked_addresses.append({"address": address, "reason": "invalid_ip"})
            continue
        if not allowed:
            blocked_addresses.append({"address": address, "reason": reason})

    if not resolved_addresses:
        return {
            "success": False,
            "error": "host_resolution_failed",
            "message": f"Remote host '{hostname}' did not resolve to any addresses.",
            "url": url,
            "host": hostname,
        }

    if blocked_addresses:
        return {
            "success": False,
            "error": "blocked_private_host",
            "message": "Remote artefact ingestion blocked a non-public destination.",
            "url": url,
            "host": hostname,
            "resolved_addresses": resolved_addresses,
            "blocked_addresses": blocked_addresses,
        }

    return {
        "success": True,
        "url": url,
        "host": hostname,
        "resolved_addresses": resolved_addresses,
    }


def _create_requests_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def download_remote_file_copy_bytes(
    *,
    url: str,
    filename: str | None = None,
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    timeout_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Fetch a remote artefact with SSRF and size guardrails."""

    if not isinstance(url, str) or not url.strip():
        return {
            "success": False,
            "error": "missing_url",
            "message": "Provide a remote URL to import.",
        }

    requested_url = url.strip()
    resolved_max_bytes = _configured_max_bytes(max_bytes)
    resolved_max_redirects = _configured_max_redirects(max_redirects)
    connect_timeout, read_timeout = _configured_timeouts()
    resolved_timeout_seconds = _configured_total_timeout_seconds(timeout_seconds)
    started_at = time.monotonic()
    deadline = started_at + resolved_timeout_seconds
    current_url = requested_url
    redirects: list[dict[str, Any]] = []
    hops: list[dict[str, Any]] = []

    def _elapsed_seconds() -> float:
        return round(max(0.0, time.monotonic() - started_at), 3)

    def _remaining_seconds() -> float:
        return deadline - time.monotonic()

    def _timeout_failure(
        *,
        final_url: str,
        status_code: int | None = None,
        size_bytes: int | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        failure: dict[str, Any] = {
            "success": False,
            "error": "remote_file_copy_timeout",
            "message": message
            or "Remote artefact ingestion exceeded the total timeout.",
            "requested_url": requested_url,
            "final_url": final_url,
            "redirects": redirects,
            "hops": hops,
            "timeout_seconds": resolved_timeout_seconds,
            "elapsed_seconds": _elapsed_seconds(),
        }
        if status_code is not None:
            failure["status_code"] = status_code
        if size_bytes is not None:
            failure["size_bytes"] = size_bytes
        return failure

    session = _create_requests_session()
    try:
        for _hop_index in range(resolved_max_redirects + 1):
            remaining = _remaining_seconds()
            if remaining <= 0:
                return _timeout_failure(final_url=current_url)

            validation = _validate_remote_target(current_url)
            if not validation.get("success"):
                failure = dict(validation)
                failure["requested_url"] = requested_url
                failure["redirects"] = redirects
                failure["hops"] = hops
                failure["timeout_seconds"] = resolved_timeout_seconds
                failure["elapsed_seconds"] = _elapsed_seconds()
                return failure
            hops.append(
                {
                    "url": current_url,
                    "host": validation.get("host"),
                    "resolved_addresses": validation.get("resolved_addresses"),
                }
            )

            try:
                remaining = _remaining_seconds()
                if remaining <= 0:
                    return _timeout_failure(final_url=current_url)
                request_timeout = (
                    min(float(connect_timeout), remaining),
                    min(float(read_timeout), remaining),
                )
                response = session.get(
                    current_url,
                    stream=True,
                    allow_redirects=False,
                    timeout=request_timeout,
                    headers={
                        "User-Agent": os.getenv(
                            "VON_REMOTE_FILE_COPY_USER_AGENT", _DEFAULT_USER_AGENT
                        ),
                        "Accept": "*/*",
                    },
                )
            except requests.Timeout as exc:
                return _timeout_failure(
                    final_url=current_url,
                    message=f"Remote artefact request timed out: {exc}",
                )
            except requests.RequestException as exc:
                return {
                    "success": False,
                    "error": "remote_request_failed",
                    "message": f"Remote artefact request failed: {exc}",
                    "requested_url": requested_url,
                    "final_url": current_url,
                    "redirects": redirects,
                    "hops": hops,
                    "timeout_seconds": resolved_timeout_seconds,
                    "elapsed_seconds": _elapsed_seconds(),
                }

            try:
                status_code = int(response.status_code)
            except Exception:
                status_code = 0

            try:
                location = response.headers.get("Location")
                if (
                    status_code in _REDIRECT_STATUS_CODES
                    and isinstance(location, str)
                    and location.strip()
                ):
                    if len(redirects) >= resolved_max_redirects:
                        return {
                            "success": False,
                            "error": "too_many_redirects",
                            "message": "Remote artefact ingestion exceeded the redirect limit.",
                            "requested_url": requested_url,
                            "final_url": current_url,
                            "redirects": redirects,
                            "hops": hops,
                            "status_code": status_code,
                        }

                    next_url = urljoin(current_url, location.strip())
                    redirects.append(
                        {
                            "status_code": status_code,
                            "from_url": current_url,
                            "to_url": next_url,
                        }
                    )
                    current_url = next_url
                    continue

                if status_code < 200 or status_code >= 300:
                    return {
                        "success": False,
                        "error": "remote_http_error",
                        "message": f"Remote artefact request returned HTTP {status_code}.",
                        "requested_url": requested_url,
                        "final_url": current_url,
                        "status_code": status_code,
                        "redirects": redirects,
                        "hops": hops,
                    }

                content_length_raw = response.headers.get("Content-Length")
                content_length: int | None = None
                if isinstance(content_length_raw, str) and content_length_raw.strip():
                    try:
                        content_length = int(content_length_raw.strip())
                    except ValueError:
                        content_length = None
                if (
                    isinstance(content_length, int)
                    and content_length > resolved_max_bytes
                ):
                    return {
                        "success": False,
                        "error": "remote_file_too_large",
                        "message": "Remote artefact exceeds the configured maximum size.",
                        "requested_url": requested_url,
                        "final_url": current_url,
                        "status_code": status_code,
                        "content_length": content_length,
                        "max_bytes": resolved_max_bytes,
                        "redirects": redirects,
                        "hops": hops,
                    }

                captured_headers = {
                    key.lower().replace("-", "_"): response.headers.get(key)
                    for key in _CAPTURED_RESPONSE_HEADERS
                    if response.headers.get(key) is not None
                }

                data = bytearray()
                try:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if _remaining_seconds() <= 0:
                            return _timeout_failure(
                                final_url=current_url,
                                status_code=status_code,
                                size_bytes=len(data),
                            )
                        if not chunk:
                            continue
                        data.extend(chunk)
                        if len(data) > resolved_max_bytes:
                            return {
                                "success": False,
                                "error": "remote_file_too_large",
                                "message": "Remote artefact exceeded the configured maximum size while downloading.",
                                "requested_url": requested_url,
                                "final_url": current_url,
                                "status_code": status_code,
                                "size_bytes": len(data),
                                "max_bytes": resolved_max_bytes,
                                "redirects": redirects,
                                "hops": hops,
                            }
                    if _remaining_seconds() <= 0:
                        return _timeout_failure(
                            final_url=current_url,
                            status_code=status_code,
                            size_bytes=len(data),
                        )
                except requests.Timeout as exc:
                    return _timeout_failure(
                        final_url=current_url,
                        status_code=status_code,
                        size_bytes=len(data),
                        message=f"Remote artefact download timed out: {exc}",
                    )
                except requests.RequestException as exc:
                    return {
                        "success": False,
                        "error": "remote_request_failed",
                        "message": f"Remote artefact download failed: {exc}",
                        "requested_url": requested_url,
                        "final_url": current_url,
                        "status_code": status_code,
                        "size_bytes": len(data),
                        "redirects": redirects,
                        "hops": hops,
                        "timeout_seconds": resolved_timeout_seconds,
                        "elapsed_seconds": _elapsed_seconds(),
                    }

                if not data:
                    return {
                        "success": False,
                        "error": "empty_remote_file",
                        "message": "Remote artefact response contained no bytes.",
                        "requested_url": requested_url,
                        "final_url": current_url,
                        "status_code": status_code,
                        "redirects": redirects,
                        "hops": hops,
                    }

                response_content_type = _normalise_content_type(
                    response.headers.get("Content-Type")
                )
                content_disposition = response.headers.get("Content-Disposition")
                original_filename, filename_source = _choose_filename(
                    requested_filename=filename,
                    content_disposition=content_disposition,
                    final_url=str(response.url or current_url),
                    content_type=response_content_type,
                )
                effective_content_type, content_type_source = _choose_content_type(
                    response_content_type=response_content_type,
                    original_filename=original_filename,
                )

                return {
                    "success": True,
                    "requested_url": requested_url,
                    "final_url": str(response.url or current_url),
                    "redirects": redirects,
                    "redirect_count": len(redirects),
                    "hops": hops,
                    "status_code": status_code,
                    "size_bytes": len(data),
                    "timeout_seconds": resolved_timeout_seconds,
                    "elapsed_seconds": _elapsed_seconds(),
                    "data": bytes(data),
                    "original_filename": original_filename,
                    "filename_source": filename_source,
                    "response_content_type": response_content_type,
                    "content_type": effective_content_type,
                    "content_type_source": content_type_source,
                    "response_headers": captured_headers,
                }
            finally:
                response.close()

        return {
            "success": False,
            "error": "too_many_redirects",
            "message": "Remote artefact ingestion exceeded the redirect limit.",
            "requested_url": requested_url,
            "final_url": current_url,
            "redirects": redirects,
            "hops": hops,
            "timeout_seconds": resolved_timeout_seconds,
            "elapsed_seconds": _elapsed_seconds(),
        }
    finally:
        session.close()


def import_remote_url_file_copy(
    *,
    url: str,
    user_concept_id: str,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    namespace_source: str | None = None,
    filename: str | None = None,
    type_concept_id: str = "#V#computer_file_copy",
    source_system: str = "remote_url_import",
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    timeout_seconds: float | int | None = None,
    registration_timeout_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Download a remote artefact, persist it durably, and register a file copy."""

    download_timeout_seconds = _configured_total_timeout_seconds(timeout_seconds)
    resolved_registration_timeout_seconds = _configured_registration_timeout_seconds(
        registration_timeout_seconds
    )
    started_at = time.monotonic()

    def _elapsed_seconds() -> float:
        return round(max(0.0, time.monotonic() - started_at), 3)

    download_result = download_remote_file_copy_bytes(
        url=url,
        filename=filename,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        timeout_seconds=download_timeout_seconds,
    )
    if not download_result.get("success"):
        return download_result

    response_headers = (
        dict(download_result.get("response_headers") or {})
        if isinstance(download_result.get("response_headers"), dict)
        else {}
    )
    redirects = (
        list(download_result.get("redirects") or [])
        if isinstance(download_result.get("redirects"), list)
        else []
    )
    import_kwargs = {
        "data": bytes(download_result["data"]),
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "namespace": namespace,
        "namespace_source": namespace_source,
        "original_filename": str(download_result["original_filename"]),
        "content_type": download_result.get("content_type"),
        "type_concept_id": type_concept_id,
        "source_system": source_system,
        "source_identifier": str(download_result["requested_url"]),
        "source_uri": str(download_result["final_url"]),
        "metadata": {
            "requested_url": str(download_result["requested_url"]),
            "final_url": str(download_result["final_url"]),
            "filename_source": str(download_result.get("filename_source") or ""),
            "content_type_source": str(
                download_result.get("content_type_source") or ""
            ),
            "response_status_code": str(download_result.get("status_code") or ""),
            "response_content_type": response_headers.get("content_type"),
            "response_content_disposition": response_headers.get("content_disposition"),
            "response_etag": response_headers.get("etag"),
            "response_last_modified": response_headers.get("last_modified"),
            "redirect_count": len(redirects),
            "redirect_chain_json": json.dumps(redirects, ensure_ascii=False),
        },
    }
    timeout_policy = {
        "download_timeout_seconds": download_timeout_seconds,
        "registration_timeout_seconds": resolved_registration_timeout_seconds,
        "download_phase": "remote_download",
        "registration_phase": "file_copy_registration",
    }
    download_context = {
        "requested_url": download_result.get("requested_url"),
        "final_url": download_result.get("final_url"),
        "redirects": redirects,
        "redirect_count": len(redirects),
        "download_hops": download_result.get("hops"),
        "timeout_seconds": download_timeout_seconds,
        "download_timeout_seconds": download_timeout_seconds,
        "registration_timeout_seconds": resolved_registration_timeout_seconds,
        "timeout_policy": timeout_policy,
        "response": {
            "status_code": download_result.get("status_code"),
            "size_bytes": download_result.get("size_bytes"),
            "headers": response_headers,
        },
        "filename_resolution": {
            "original_filename": download_result.get("original_filename"),
            "source": download_result.get("filename_source"),
        },
        "content_type_resolution": {
            "effective_content_type": download_result.get("content_type"),
            "response_content_type": download_result.get("response_content_type"),
            "source": download_result.get("content_type_source"),
        },
    }

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="remote-file-copy-register",
    )
    future = executor.submit(import_bytes_file_copy, **import_kwargs)
    try:
        import_result = future.result(
            timeout=max(0.001, resolved_registration_timeout_seconds)
        )
    except concurrent.futures.TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return {
            "success": False,
            "error": "remote_file_copy_timeout",
            "message": (
                "Remote artefact file-copy registration exceeded the registration "
                "timeout after the download completed."
            ),
            "timeout_phase": "file_copy_registration",
            "elapsed_seconds": _elapsed_seconds(),
            "download": {
                "status": "completed",
                "timeout_seconds": download_timeout_seconds,
                "status_code": download_result.get("status_code"),
                "size_bytes": download_result.get("size_bytes"),
            },
            "registration": {
                "status": "timed_out",
                "timeout_seconds": resolved_registration_timeout_seconds,
                "may_complete_late": True,
                "recovery_affordance": (
                    "Read back the file-copy result or retry the registration phase "
                    "before re-downloading the remote artefact."
                ),
            },
            **download_context,
        }
    finally:
        if future.done():
            executor.shutdown(wait=True)

    if not import_result.get("success"):
        merged_failure = dict(import_result)
        merged_failure.update(
            {
                "requested_url": download_result.get("requested_url"),
                "final_url": download_result.get("final_url"),
                "redirects": redirects,
                "redirect_count": len(redirects),
                "response": {
                    "status_code": download_result.get("status_code"),
                    "size_bytes": download_result.get("size_bytes"),
                    "headers": response_headers,
                },
                "filename_resolution": {
                    "original_filename": download_result.get("original_filename"),
                    "source": download_result.get("filename_source"),
                },
                "content_type_resolution": {
                    "effective_content_type": download_result.get("content_type"),
                    "response_content_type": download_result.get(
                        "response_content_type"
                    ),
                    "source": download_result.get("content_type_source"),
                },
                "elapsed_seconds": _elapsed_seconds(),
                "download_timeout_seconds": download_timeout_seconds,
                "registration_timeout_seconds": resolved_registration_timeout_seconds,
                "timeout_policy": timeout_policy,
            }
        )
        return merged_failure

    result = dict(import_result)
    result.update(
        {
            **download_context,
            "elapsed_seconds": _elapsed_seconds(),
        }
    )
    return result


__all__ = [
    "download_remote_file_copy_bytes",
    "import_remote_url_file_copy",
]
