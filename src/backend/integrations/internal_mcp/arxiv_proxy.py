"""arXiv MCP proxy for internal gateway.

Manages subprocess communication with external arxiv-mcp-server.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from src.backend.services.blob_store import get_blob_store_from_env

logger = logging.getLogger(__name__)

_LOG_TAG = "[arxiv_proxy]"


@dataclass
class ArxivProxyConfig:
    """Configuration for arXiv MCP subprocess."""

    storage_path: Path
    command: str = "uv"
    timeout_sec: float = 30.0


class ArxivProxyError(Exception):
    """Raised when arXiv proxy operations fail."""


class ArxivMCPProxy:
    """Manages external arxiv-mcp-server subprocess and tool invocation."""

    def __init__(self, config: ArxivProxyConfig):
        self._config = config
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._call_count = 0
        self._error_count = 0

    def _ensure_storage_path(self) -> None:
        """Create storage directory if it doesn't exist."""
        self._config.storage_path.mkdir(parents=True, exist_ok=True)

    def _start_process(self) -> subprocess.Popen:
        """Start the arxiv-mcp-server subprocess."""
        self._ensure_storage_path()

        args = [
            self._config.command,
            "tool",
            "run",
            "arxiv-mcp-server",
            "--storage-path",
            str(self._config.storage_path),
        ]

        logger.info("%s Starting subprocess: %s", _LOG_TAG, " ".join(args))

        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            return process
        except FileNotFoundError as e:
            raise ArxivProxyError(
                f"Failed to start arxiv-mcp-server: {e}. "
                f"Ensure 'uv' and 'arxiv-mcp-server' are installed."
            ) from e
        except Exception as e:
            raise ArxivProxyError(f"Failed to start arxiv-mcp-server: {e}") from e

    def _send_request(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Send MCP request to subprocess and get response."""
        with self._lock:
            self._call_count += 1

            # Start process if not running
            if self._process is None or self._process.poll() is not None:
                logger.info("%s Process not running, starting new instance", _LOG_TAG)
                self._process = self._start_process()

            # Build MCP request (simplified stdio format)
            request = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
                "id": self._call_count,
            }

            try:
                # Send request
                request_json = json.dumps(request) + "\n"
                logger.debug("%s Sending request: %s", _LOG_TAG, request_json.strip())

                if self._process.stdin is None:
                    raise ArxivProxyError("Process stdin is None")

                self._process.stdin.write(request_json)
                self._process.stdin.flush()

                # Read response
                if self._process.stdout is None:
                    raise ArxivProxyError("Process stdout is None")

                response_line = self._process.stdout.readline()
                if not response_line:
                    raise ArxivProxyError("No response from arxiv-mcp-server")

                logger.debug(
                    "%s Received response: %s", _LOG_TAG, response_line.strip()
                )
                response = json.loads(response_line)

                # Check for error
                if "error" in response:
                    error = response["error"]
                    error_msg = error.get("message", "Unknown error")
                    self._error_count += 1
                    raise ArxivProxyError(f"arXiv MCP error: {error_msg}")

                # Extract result
                if "result" not in response:
                    raise ArxivProxyError("Invalid response: missing 'result' field")

                return response["result"]

            except json.JSONDecodeError as e:
                self._error_count += 1
                raise ArxivProxyError(f"Failed to parse response: {e}") from e
            except BrokenPipeError as e:
                self._error_count += 1
                self._process = None  # Mark for restart
                raise ArxivProxyError(
                    "Subprocess pipe broken, will restart on next call"
                ) from e
            except Exception as e:
                self._error_count += 1
                raise ArxivProxyError(f"Request failed: {e}") from e

    def search_arxiv(
        self,
        query: str,
        max_results: int = 10,
        sort_by: str = "relevance",
        sort_order: str = "descending",
    ) -> Dict[str, Any]:
        """Search arXiv by query.

        Args:
            query: Search query (supports boolean operators)
            max_results: Maximum results to return
            sort_by: Sort order - "relevance" or "date"
            sort_order: Ignored (kept for backwards compatibility)

        Returns:
            Dict with search results
        """
        # External arxiv-mcp-server expects 'search_papers' tool name
        # and does not support sort_order parameter
        return self._send_request(
            "search_papers",
            {
                "query": query,
                "max_results": max_results,
                "sort_by": sort_by,
            },
        )

    def get_paper_metadata(self, arxiv_id: str) -> Dict[str, Any]:
        """Get metadata for a specific arXiv paper.

        Note: This is not a standard arxiv-mcp-server tool.
        May need to be implemented differently or removed.

        Args:
            arxiv_id: arXiv identifier (e.g., "2506.16596" or "arXiv:2506.16596")

        Returns:
            Dict with paper metadata (title, authors, abstract, etc.)
        """
        return self._send_request("get_paper_metadata", {"arxiv_id": arxiv_id})

    def download_paper(
        self, arxiv_id: str, filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """Download arXiv paper PDF to storage.

        Args:
            arxiv_id: arXiv identifier
            filename: Optional custom filename (not supported by external server)

        Returns:
            Dict with download status and file path
        """
        # External arxiv-mcp-server expects 'paper_id' parameter, not 'arxiv_id'
        arguments = {"paper_id": arxiv_id}
        # Note: filename parameter not supported by external server
        if filename:
            logger.warning(
                "%s filename parameter not supported by arxiv-mcp-server, ignoring",
                _LOG_TAG,
            )

        result = self._send_request("download_paper", arguments)
        return self._store_downloaded_pdf(result=result, arxiv_id=arxiv_id)

    def _store_downloaded_pdf(
        self, *, result: Dict[str, Any], arxiv_id: str
    ) -> Dict[str, Any]:
        file_path = _extract_download_file_path(result)
        if not file_path:
            blob_bytes = _extract_download_blob_bytes(result)
            if blob_bytes:
                safe_id = _normalise_arxiv_id(arxiv_id).replace("/", "_")
                path = self._config.storage_path / f"{safe_id}.pdf"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob_bytes)
                file_path = str(path)
                result = dict(result)
                result["file_path"] = file_path
            else:
                raise ArxivProxyError(
                    "arXiv download succeeded but no file path was returned by arxiv-mcp-server"
                )

        path = Path(file_path)
        if not path.is_absolute():
            # Some tools return a relative path; treat it as relative to the configured cache dir.
            path = self._config.storage_path / path

        if not path.exists():
            raise ArxivProxyError(f"Downloaded PDF not found at: {path}")

        size_bytes = path.stat().st_size

        blob_store = get_blob_store_from_env()
        storage_key = _arxiv_pdf_blob_key(arxiv_id)

        try:
            data = path.read_bytes()
            sha256 = hashlib.sha256(data).hexdigest()
            ref = blob_store.put_bytes(
                storage_key,
                data,
                content_type="application/pdf",
                metadata={
                    "source": "arxiv",
                    "arxiv_id": _normalise_arxiv_id(arxiv_id),
                    "original_path": str(path),
                    "sha256": sha256,
                    "size_bytes": str(size_bytes),
                },
            )
        except Exception as exc:
            raise ArxivProxyError(f"Failed to store PDF in blob store: {exc}") from exc

        stored = dict(result)
        stored["arxiv_id"] = arxiv_id
        stored["file_path"] = str(path)
        stored["size_bytes"] = size_bytes
        stored["sha256"] = sha256
        stored["version"] = _extract_arxiv_version(arxiv_id)
        stored["storage"] = {
            "backend": ref.backend,
            "key": ref.key,
            "uri": ref.uri,
        }
        return stored

    def list_papers(self) -> Dict[str, Any]:
        """List cached PDFs without calling upstream arXiv.

        Some arxiv-mcp-server versions issue an invalid arXiv API request when
        listing with no filters; this local listing avoids that failure.
        """
        self._ensure_storage_path()

        papers: list[Dict[str, Any]] = []
        for path in sorted(self._config.storage_path.rglob("*.pdf")):
            if not path.is_file():
                continue
            arxiv_id = _guess_arxiv_id_from_filename(path.name)
            papers.append(
                {
                    "file_path": str(path),
                    "filename": path.name,
                    "size_bytes": path.stat().st_size,
                    "arxiv_id": arxiv_id,
                    "version": _extract_arxiv_version(arxiv_id) if arxiv_id else None,
                }
            )

        return {
            "success": True,
            "total_papers": len(papers),
            "papers": papers,
        }

    def shutdown(self) -> None:
        """Terminate the subprocess gracefully."""
        with self._lock:
            if self._process is not None:
                logger.info("%s Shutting down subprocess", _LOG_TAG)
                try:
                    self._process.terminate()
                    self._process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    logger.warning("%s Process did not terminate, killing", _LOG_TAG)
                    self._process.kill()
                    self._process.wait()
                except Exception as e:
                    logger.error("%s Error during shutdown: %s", _LOG_TAG, e)
                finally:
                    self._process = None

    def get_diagnostics(self) -> Dict[str, Any]:
        """Return proxy diagnostics."""
        with self._lock:
            return {
                "running": self._process is not None and self._process.poll() is None,
                "call_count": self._call_count,
                "error_count": self._error_count,
                "storage_path": str(self._config.storage_path),
            }


# Global proxy instance (lazy-initialized)
_proxy_instance: Optional[ArxivMCPProxy] = None
_proxy_lock = threading.Lock()


def get_arxiv_proxy() -> ArxivMCPProxy:
    """Get or create the global arXiv proxy instance."""
    global _proxy_instance

    with _proxy_lock:
        if _proxy_instance is None:
            # Determine storage path
            import os

            workspace_root = Path(__file__).parent.parent.parent.parent.parent
            # This is a cache directory for the external arxiv-mcp-server. The durable
            # storage location is the blob store (local, Swift, or S3-compatible).
            storage_path = workspace_root / "data" / "arxiv_cache"

            # Allow override via environment variable
            env_storage = os.environ.get("ARXIV_CACHE_PATH") or os.environ.get(
                "ARXIV_STORAGE_PATH"
            )
            if env_storage:
                storage_path = Path(env_storage)

            config = ArxivProxyConfig(storage_path=storage_path)
            _proxy_instance = ArxivMCPProxy(config)
            logger.info(
                "%s Initialized arXiv proxy with storage: %s", _LOG_TAG, storage_path
            )

        return _proxy_instance


def shutdown_arxiv_proxy() -> None:
    """Shutdown the global proxy instance."""
    global _proxy_instance

    with _proxy_lock:
        if _proxy_instance is not None:
            _proxy_instance.shutdown()
            _proxy_instance = None


def _normalise_arxiv_id(arxiv_id: str) -> str:
    value = arxiv_id.strip()
    if value.lower().startswith("arxiv:"):
        value = value.split(":", 1)[1].strip()
    return value


def _arxiv_pdf_blob_key(arxiv_id: str) -> str:
    safe = _normalise_arxiv_id(arxiv_id).replace("/", "_")
    return f"arxiv/papers/{safe}.pdf"


def _coerce_mapping(value: Any) -> Dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return None
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            return None
    return None


def _normalise_path_candidate(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None

    if candidate.startswith("file://"):
        import re
        from urllib.parse import urlparse, unquote

        parsed = urlparse(candidate)
        if parsed.scheme != "file":
            return None
        path = unquote(parsed.path or "")
        netloc = unquote(parsed.netloc or "")
        if re.match(r"^[A-Za-z]:$", netloc):
            path = f"{netloc}{path}"
        elif re.match(r"^[A-Za-z]:[\\\\/]", netloc):
            path = netloc + path
        elif netloc and netloc not in {"", "localhost"}:
            path = f"//{netloc}{path}"
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        if path and path.lower().endswith(".md"):
            path = path[:-3] + ".pdf"
        return path or None

    if "://" in candidate:
        return None

    if candidate.lower().endswith(".pdf"):
        return candidate

    if ".pdf" in candidate:
        import re

        match = re.search(
            r"([A-Za-z]:[\\\\/][^\\s]+\\.pdf|/[^\\s]+\\.pdf|[^\\s]+\\.pdf)",
            candidate,
        )
        if match:
            return match.group(1)

    if candidate.lower().endswith(".md"):
        return candidate[:-3] + ".pdf"

    return None


def _extract_download_file_path(result: Dict[str, Any]) -> str | None:
    stack: list[Any] = [result]
    seen: set[int] = set()

    while stack:
        item = stack.pop()
        item_id = id(item)
        if item_id in seen:
            continue
        seen.add(item_id)

        if isinstance(item, str):
            maybe = _normalise_path_candidate(item)
            if maybe:
                return maybe
            continue

        mapping = _coerce_mapping(item)
        if mapping is not None:
            for value in mapping.values():
                if isinstance(value, str):
                    maybe = _normalise_path_candidate(value)
                    if maybe:
                        return maybe
                if isinstance(value, (dict, list, tuple)) or _coerce_mapping(value):
                    stack.append(value)
            continue

        if isinstance(item, (list, tuple)):
            stack.extend(item)

    return None


def _extract_download_blob_bytes(result: Dict[str, Any]) -> bytes | None:
    stack: list[Any] = [result]
    seen: set[int] = set()

    while stack:
        item = stack.pop()
        item_id = id(item)
        if item_id in seen:
            continue
        seen.add(item_id)

        mapping = _coerce_mapping(item)
        if mapping is not None:
            blob_value = mapping.get("blob")
            if isinstance(blob_value, str) and blob_value.strip():
                import base64

                try:
                    return base64.b64decode(blob_value)
                except Exception:
                    return None

            for value in mapping.values():
                if isinstance(value, (dict, list, tuple)) or _coerce_mapping(value):
                    stack.append(value)
            continue

        if isinstance(item, (list, tuple)):
            stack.extend(item)

    return None


def _guess_arxiv_id_from_filename(filename: str) -> str | None:
    name = filename.strip()
    if not name:
        return None

    if name.lower().endswith(".pdf"):
        name = name[:-4]

    for prefix in ("arxiv_", "arxiv-", "arxiv:"):
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
            break

    name = name.strip()
    return name or None


def _extract_arxiv_version(arxiv_id: str | None) -> int | None:
    if not arxiv_id:
        return None

    value = _normalise_arxiv_id(arxiv_id)
    lower = value.lower()
    idx = lower.rfind("v")
    if idx <= 0:
        return None

    suffix = value[idx + 1 :]
    if not suffix.isdigit():
        return None

    try:
        return int(suffix)
    except Exception:
        return None
