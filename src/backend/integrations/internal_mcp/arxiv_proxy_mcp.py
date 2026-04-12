"""arXiv MCP proxy using official MCP SDK client.

Manages subprocess communication with external arxiv-mcp-server using the MCP protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, List

from src.backend.services.blob_store import get_blob_store_from_env
from src.backend.services.blob_uploads import BlobUploadError, put_bytes_durable

from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession
from mcp import types as mcp_types

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
    """Manages external arxiv-mcp-server subprocess using MCP SDK client."""

    def __init__(self, config: ArxivProxyConfig):
        self._config = config
        self._call_count = 0
        self._error_count = 0

    def _ensure_storage_path(self) -> None:
        """Create storage directory if it doesn't exist."""
        self._config.storage_path.mkdir(parents=True, exist_ok=True)

    def _get_server_params(self) -> StdioServerParameters:
        """Get server parameters for stdio client."""
        self._ensure_storage_path()
        return StdioServerParameters(
            command=self._config.command,
            args=[
                "tool",
                "run",
                "arxiv-mcp-server",
                "--storage-path",
                str(self._config.storage_path),
            ],
            env=None,
        )

    async def _call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call an MCP tool and return the result.

        Creates a new MCP session for each tool call to avoid lifecycle issues.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool result (parsed from response)

        Raises:
            ArxivProxyError: If tool call fails
        """
        server_params = self._get_server_params()

        logger.info(
            "%s Calling tool %s with arguments %s",
            _LOG_TAG,
            tool_name,
            arguments,
        )

        try:
            # Create a new session for this call
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    # Initialize session
                    await session.initialize()

                    # Call the tool
                    result = await session.call_tool(tool_name, arguments)

                    self._call_count += 1

                    # Extract content from response
                    if not result.content:
                        return {}

                    payloads: list[dict[str, Any]] = []
                    first_text: str | None = None
                    for item in result.content:
                        if isinstance(item, mcp_types.TextContent):
                            text_data = item.text
                            if first_text is None:
                                first_text = text_data
                            payloads.append({"type": "text", "text": text_data})
                        elif isinstance(item, mcp_types.ImageContent):
                            payloads.append(
                                {
                                    "type": "image",
                                    "image": item.data,
                                    "mimeType": item.mimeType,
                                }
                            )
                        elif isinstance(item, mcp_types.EmbeddedResource):
                            payloads.append(
                                {
                                    "type": "resource",
                                    "resource": item.resource,
                                }
                            )

                    if len(payloads) == 1 and payloads[0].get("type") == "text":
                        text_data = payloads[0].get("text") or ""
                        if str(text_data).strip().startswith("{"):
                            try:
                                import json

                                return json.loads(text_data)
                            except json.JSONDecodeError:
                                return {"text": text_data}
                        return {"text": text_data}

                    combined: dict[str, Any] = {"items": payloads}
                    if first_text is not None:
                        combined["text"] = first_text
                    return combined

        except Exception as e:
            self._error_count += 1
            logger.error("%s Tool call failed: %s", _LOG_TAG, e)
            raise ArxivProxyError(f"Request failed: {e}") from e

    async def search_arxiv(
        self,
        query: str,
        max_results: int = 10,
        sort_by: str = "relevance",
        sort_order: str = "descending",  # Kept for backward compatibility, not used
    ) -> Dict[str, Any]:
        """Search arXiv papers.

        Args:
            query: Search query
            max_results: Maximum number of results
            sort_by: Sort criterion ('relevance' or 'date')
            sort_order: Ignored (kept for backwards compatibility)

        Returns:
            Dict with search results
        """
        # External arxiv-mcp-server expects 'search_papers' tool name
        return await self._call_tool(
            "search_papers",
            {
                "query": query,
                "max_results": max_results,
                "sort_by": sort_by,
            },
        )

    async def get_paper_metadata(self, arxiv_id: str) -> Dict[str, Any]:
        """Get metadata for a specific arXiv paper.

        Note: This tool may not be available in arxiv-mcp-server.
        Consider using search with the arXiv ID instead.

        Args:
            arxiv_id: arXiv identifier

        Returns:
            Dict with paper metadata
        """
        return await self._call_tool("get_paper_metadata", {"arxiv_id": arxiv_id})

    async def download_paper(
        self, arxiv_id: str, filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """Download arXiv paper PDF to storage.

        Args:
            arxiv_id: arXiv identifier
            filename: Optional custom filename (not supported by external server)

        Returns:
            Dict with download status and file path
        """
        # External arxiv-mcp-server expects 'paper_id' parameter
        arguments = {"paper_id": arxiv_id}
        if filename:
            logger.warning(
                "%s filename parameter not supported by arxiv-mcp-server, ignoring",
                _LOG_TAG,
            )

        started_at = time.time()
        result = await self._call_tool("download_paper", arguments)
        return self._store_downloaded_pdf(
            result=result, arxiv_id=arxiv_id, download_started_at=started_at
        )

    def _store_downloaded_pdf(
        self,
        *,
        result: Any,
        arxiv_id: str,
        download_started_at: float | None = None,
    ) -> Dict[str, Any]:
        if (
            isinstance(result, dict)
            and "text" in result
            and isinstance(result["text"], str)
        ):
            # Some versions return a single text payload.
            try:
                import json

                parsed = json.loads(result["text"])
                if isinstance(parsed, dict):
                    result = parsed
            except Exception:
                pass

        if not isinstance(result, dict):
            raise ArxivProxyError(
                f"Unexpected download_paper result type: {type(result).__name__}"
            )

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
                cached = _find_cached_pdf_for_arxiv_id(
                    self._config.storage_path, arxiv_id
                )
                if cached is not None:
                    file_path = str(cached)
                    result = dict(result)
                    result["file_path"] = file_path
                    logger.warning(
                        "%s download_paper returned no file path; using cached PDF at %s",
                        _LOG_TAG,
                        cached,
                    )
                else:
                    waited = _await_downloaded_pdf_in_cache(
                        self._config.storage_path,
                        arxiv_id=arxiv_id,
                        since=download_started_at,
                        timeout_sec=self._config.timeout_sec,
                        poll_interval_sec=0.5,
                        allow_recent_fallback=_download_result_indicates_async_settlement(
                            result
                        ),
                    )
                    if waited is not None:
                        file_path = str(waited)
                        result = dict(result)
                        result["file_path"] = file_path
                        logger.warning(
                            "%s download_paper returned no file path; settled on cached PDF at %s",
                            _LOG_TAG,
                            waited,
                        )
                    else:
                        raise ArxivProxyError(
                            "arXiv download succeeded but no file path was returned by arxiv-mcp-server"
                        )

        path = Path(file_path)
        if not path.is_absolute():
            path = self._config.storage_path / path

        if not path.exists():
            raise ArxivProxyError(f"Downloaded PDF not found at: {path}")

        size_bytes = path.stat().st_size
        storage_key = _arxiv_pdf_blob_key(arxiv_id)

        try:
            data = path.read_bytes()
            sha256 = hashlib.sha256(data).hexdigest()
            stored = put_bytes_durable(
                key=storage_key,
                data=data,
                content_type="application/pdf",
                sha256=sha256,
                size_bytes=size_bytes,
                metadata={
                    "source": "arxiv",
                    "arxiv_id": _normalise_arxiv_id(arxiv_id),
                    "original_path": str(path),
                },
            )
            ref = stored.ref
        except BlobUploadError as exc:
            raise ArxivProxyError(f"Failed to store PDF in blob store: {exc}") from exc

        stored = dict(result)
        stored["success"] = True
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

    async def list_papers(self) -> Dict[str, Any]:
        """List all downloaded papers.

        Returns:
            Dict with list of papers
        """
        # Avoid calling arXiv upstream with an empty query (some tool versions issue
        # an invalid arXiv API request when no filters are provided).
        self._ensure_storage_path()

        by_id: dict[str, Dict[str, Any]] = {}

        # 1) Local cache listing (external arxiv-mcp-server cache).
        for path in sorted(self._config.storage_path.rglob("*.pdf")):
            if not path.is_file():
                continue

            filename = path.name
            arxiv_id = _guess_arxiv_id_from_filename(filename)
            stable_id = _normalise_arxiv_id(arxiv_id) if arxiv_id else filename
            entry = by_id.setdefault(
                stable_id,
                {
                    "arxiv_id": arxiv_id,
                    "filename": filename,
                    "version": _extract_arxiv_version(arxiv_id) if arxiv_id else None,
                },
            )

            entry["file_path"] = str(path)
            entry["size_bytes"] = path.stat().st_size

        # 2) Durable listing (blob store - local, Swift, or S3-compatible).
        # Included by default; set VON_ARXIV_INCLUDE_DURABLE_LISTING=0 to opt out.
        env_value = os.environ.get("VON_ARXIV_INCLUDE_DURABLE_LISTING")
        env_configured = True
        if env_value is not None:
            env_normalised = env_value.strip().lower()
            env_configured = env_normalised not in {"0", "false", "no", "off"}

        blob_store = None
        if env_configured:
            try:
                blob_store = get_blob_store_from_env()
            except Exception:
                blob_store = None

        if blob_store is not None:
            try:
                durable_keys = blob_store.list("arxiv/papers")
            except Exception:
                durable_keys = []

            backend = _infer_blob_backend(blob_store)
            for key in durable_keys:
                if not isinstance(key, str) or not key.lower().endswith(".pdf"):
                    continue

                arxiv_id = _guess_arxiv_id_from_blob_key(key)
                stable_id = _normalise_arxiv_id(arxiv_id) if arxiv_id else key

                filename = os.path.basename(key)
                entry = by_id.setdefault(
                    stable_id,
                    {
                        "arxiv_id": arxiv_id,
                        "filename": filename,
                        "version": (
                            _extract_arxiv_version(arxiv_id) if arxiv_id else None
                        ),
                    },
                )

                entry["storage"] = {
                    "backend": backend,
                    "key": key,
                    "uri": _build_blob_uri(blob_store, backend=backend, key=key),
                }

                # If durable store is local, we can compute size cheaply.
                if backend == "local" and "size_bytes" not in entry:
                    size = _try_local_blob_size(blob_store, key)
                    if size is not None:
                        entry["size_bytes"] = size

        papers: List[Dict[str, Any]] = sorted(
            by_id.values(),
            key=lambda p: str(p.get("arxiv_id") or p.get("filename") or ""),
        )

        return {
            "success": True,
            "total_papers": len(papers),
            "papers": papers,
        }

    async def read_paper(self, arxiv_id: str) -> Dict[str, Any]:
        """Read content of a downloaded paper.

        Args:
            arxiv_id: arXiv identifier

        Returns:
            Dict with paper content
        """
        return await self._call_tool("read_paper", {"paper_id": arxiv_id})

    def get_stats(self) -> Dict[str, int]:
        """Get proxy statistics.

        Returns:
            Dict with call_count and error_count
        """
        return {
            "call_count": self._call_count,
            "error_count": self._error_count,
        }


def _infer_blob_backend(blob_store: Any) -> str:
    name = getattr(blob_store, "__class__", type("x", (), {})).__name__
    if name == "SwiftBlobStore":
        return "swift"
    if name == "S3BlobStore":
        return "s3"
    if name == "LocalBlobStore":
        return "local"
    return (os.environ.get("VON_BLOB_STORE_BACKEND") or "local").strip().lower()


def _build_blob_uri(blob_store: Any, *, backend: str, key: str) -> str | None:
    if backend == "local":
        root_dir = getattr(blob_store, "root_dir", None)
        if root_dir is None:
            return None
        try:
            return str(Path(root_dir) / key)
        except Exception:
            return None

    if backend == "swift":
        container = (os.environ.get("VON_SWIFT_CONTAINER") or "").strip()
        if not container:
            return None
        prefix = (os.environ.get("VON_SWIFT_PREFIX") or "").strip("/")
        public_base_url = os.environ.get("VON_SWIFT_PUBLIC_BASE_URL")
        public_base_url = public_base_url.rstrip("/") if public_base_url else None

        full_key = _normalise_key_for_uri(key)
        if prefix:
            full_key = f"{prefix}/{full_key}"

        if public_base_url:
            return f"{public_base_url}/{container}/{full_key}"
        return f"swift://{container}/{full_key}"

    if backend == "s3":
        bucket = (
            (os.environ.get("VON_S3_BUCKET") or "").strip()
            or (os.environ.get("VON_SWIFT_CONTAINER") or "").strip()
        )
        if not bucket:
            return None
        prefix = (
            (os.environ.get("VON_S3_PREFIX") or "").strip("/")
            or (os.environ.get("VON_SWIFT_PREFIX") or "").strip("/")
        )
        public_base_url = os.environ.get("VON_S3_PUBLIC_BASE_URL")
        public_base_url = public_base_url.rstrip("/") if public_base_url else None
        endpoint_url = os.environ.get("VON_S3_ENDPOINT_URL")
        endpoint_url = endpoint_url.rstrip("/") if endpoint_url else None

        full_key = _normalise_key_for_uri(key)
        if prefix:
            full_key = f"{prefix}/{full_key}"

        if public_base_url:
            return f"{public_base_url}/{bucket}/{full_key}"
        if endpoint_url:
            return f"{endpoint_url}/{bucket}/{full_key}"
        return f"s3://{bucket}/{full_key}"

    return None


def _normalise_key_for_uri(key: str) -> str:
    return key.strip().replace("\\", "/").lstrip("/")


def _try_local_blob_size(blob_store: Any, key: str) -> int | None:
    root_dir = getattr(blob_store, "root_dir", None)
    if root_dir is None:
        return None
    try:
        path = Path(root_dir) / key
        if path.exists():
            return path.stat().st_size
    except Exception:
        return None
    return None


def _guess_arxiv_id_from_blob_key(key: str) -> str | None:
    value = key.replace("\\", "/")
    if value.lower().startswith("arxiv/papers/"):
        value = value[len("arxiv/papers/") :]
    return _guess_arxiv_id_from_filename(value)


def _find_cached_pdf_for_arxiv_id(storage_path: Path, arxiv_id: str) -> Path | None:
    """Best-effort fallback: locate an arXiv PDF in the cache directory.

    This is used when arxiv-mcp-server reports success but omits a file path.
    """
    try:
        storage_path = Path(storage_path)
    except Exception:
        return None
    if not storage_path.exists():
        return None

    stable_id = _normalise_arxiv_id(arxiv_id)
    safe_id = stable_id.replace("/", "_")
    candidates: list[Path] = []

    # Common filenames.
    for stem in {stable_id, safe_id, f"arxiv_{stable_id}", f"arxiv_{safe_id}"}:
        p = storage_path / f"{stem}.pdf"
        if p.exists() and p.is_file():
            candidates.append(p)

    # If arxiv_id includes a version, some tools strip/keep it differently.
    version = _extract_arxiv_version(stable_id)
    if version is not None and "v" in stable_id.lower():
        base_id = stable_id[: stable_id.lower().rfind("v")]
        base_safe = base_id.replace("/", "_")
        for stem in {base_id, base_safe, f"arxiv_{base_id}", f"arxiv_{base_safe}"}:
            p = storage_path / f"{stem}.pdf"
            if p.exists() and p.is_file():
                candidates.append(p)

    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    # Last resort: scan for PDFs containing the id in the name.
    try:
        needle = safe_id.lower()
        best: Path | None = None
        best_mtime = -1.0
        for p in storage_path.rglob("*.pdf"):
            if not p.is_file():
                continue
            if needle not in p.name.lower():
                continue
            mtime = p.stat().st_mtime
            if mtime > best_mtime:
                best = p
                best_mtime = mtime
        return best
    except Exception:
        return None


def _find_cached_markdown_for_arxiv_id(
    storage_path: Path, arxiv_id: str
) -> Path | None:
    """Best-effort fallback: locate an arXiv Markdown file in the cache directory."""

    try:
        storage_path = Path(storage_path)
    except Exception:
        return None
    if not storage_path.exists():
        return None

    stable_id = _normalise_arxiv_id(arxiv_id)
    safe_id = stable_id.replace("/", "_")
    candidates: list[Path] = []

    for stem in {stable_id, safe_id, f"arxiv_{stable_id}", f"arxiv_{safe_id}"}:
        p = storage_path / f"{stem}.md"
        if p.exists() and p.is_file():
            candidates.append(p)

    version = _extract_arxiv_version(stable_id)
    if version is not None and "v" in stable_id.lower():
        base_id = stable_id[: stable_id.lower().rfind("v")]
        base_safe = base_id.replace("/", "_")
        for stem in {base_id, base_safe, f"arxiv_{base_id}", f"arxiv_{base_safe}"}:
            p = storage_path / f"{stem}.md"
            if p.exists() and p.is_file():
                candidates.append(p)

    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    try:
        needle = safe_id.lower()
        best: Path | None = None
        best_mtime = -1.0
        for p in storage_path.rglob("*.md"):
            if not p.is_file():
                continue
            if needle not in p.name.lower():
                continue
            mtime = p.stat().st_mtime
            if mtime > best_mtime:
                best = p
                best_mtime = mtime
        return best
    except Exception:
        return None


def _find_recent_pdf_in_cache(
    storage_path: Path, *, since: float, max_age_sec: float = 180.0
) -> Path | None:
    try:
        storage_path = Path(storage_path)
    except Exception:
        return None

    if not storage_path.exists():
        return None

    now = time.time()
    cutoff = max(since - 2.0, now - max_age_sec)
    best: Path | None = None
    best_mtime = cutoff

    try:
        for p in storage_path.rglob("*.pdf"):
            if not p.is_file():
                continue
            mtime = p.stat().st_mtime
            if mtime < cutoff:
                continue
            if mtime > best_mtime:
                best = p
                best_mtime = mtime
    except Exception:
        return None

    return best


def _download_result_indicates_async_settlement(result: Any) -> bool:
    if not isinstance(result, dict):
        return False

    text_parts = [
        str(result.get("status") or "").strip(),
        str(result.get("message") or "").strip(),
        str(result.get("stage") or "").strip(),
    ]
    combined = " ".join(part for part in text_parts if part).casefold()
    if not combined:
        return False
    markers = (
        "converting",
        "conversion started",
        "processing",
        "queued",
        "downloaded",
    )
    return any(marker in combined for marker in markers)


def _await_downloaded_pdf_in_cache(
    storage_path: Path,
    *,
    arxiv_id: str,
    since: float | None,
    timeout_sec: float,
    poll_interval_sec: float = 0.5,
    allow_recent_fallback: bool = False,
) -> Path | None:
    if timeout_sec <= 0:
        return None

    deadline = time.time() + timeout_sec
    while True:
        cached = _find_cached_pdf_for_arxiv_id(storage_path, arxiv_id)
        if cached is not None:
            return cached

        if allow_recent_fallback and since is not None:
            recent = _find_recent_pdf_in_cache(storage_path, since=since)
            if recent is not None:
                return recent

        if time.time() >= deadline:
            return None

        time.sleep(max(0.05, poll_interval_sec))


def resolve_arxiv_cache_root() -> Path:
    """Return the configured local cache directory used by arxiv-mcp-server."""

    workspace_root = Path(__file__).parent.parent.parent.parent.parent
    storage_path = workspace_root / "data" / "arxiv_cache"
    env_storage = os.environ.get("ARXIV_CACHE_PATH") or os.environ.get(
        "ARXIV_STORAGE_PATH"
    )
    if env_storage:
        storage_path = Path(env_storage)
    return storage_path


def inspect_cached_arxiv_artifacts(
    *, arxiv_id: str, storage_path: Path | None = None
) -> Dict[str, Any]:
    """Return a stable snapshot of local cache state for an arXiv identifier."""

    cache_root = (
        Path(storage_path) if storage_path is not None else resolve_arxiv_cache_root()
    )
    stable_id = _normalise_arxiv_id(arxiv_id)
    cached_pdf = _find_cached_pdf_for_arxiv_id(cache_root, stable_id)
    cached_markdown = _find_cached_markdown_for_arxiv_id(cache_root, stable_id)
    has_cached_pdf = cached_pdf is not None
    has_cached_markdown = cached_markdown is not None
    partial_cache_without_pdf = has_cached_markdown and not has_cached_pdf
    if has_cached_pdf:
        cache_state = "cached_pdf_available"
    elif partial_cache_without_pdf:
        cache_state = "markdown_only_partial_cache"
    else:
        cache_state = "cache_miss"

    return {
        "schema_version": "arxiv_cache_state.v1",
        "arxiv_id": stable_id,
        "cache_root": str(cache_root),
        "cache_root_exists": cache_root.exists(),
        "cache_state": cache_state,
        "has_cached_pdf": has_cached_pdf,
        "cached_pdf_path": str(cached_pdf) if cached_pdf is not None else None,
        "has_cached_markdown": has_cached_markdown,
        "cached_markdown_path": (
            str(cached_markdown) if cached_markdown is not None else None
        ),
        "partial_cache_without_pdf": partial_cache_without_pdf,
    }


# Singleton instance
_proxy_instance: Optional[ArxivMCPProxy] = None
_proxy_lock = asyncio.Lock()


async def get_arxiv_proxy() -> ArxivMCPProxy:
    """Get or create the global arXiv proxy instance."""
    global _proxy_instance

    async with _proxy_lock:
        if _proxy_instance is None:
            storage_path = resolve_arxiv_cache_root()
            config = ArxivProxyConfig(storage_path=storage_path)
            _proxy_instance = ArxivMCPProxy(config)
            logger.info(
                "%s Initialized arXiv proxy with storage: %s", _LOG_TAG, storage_path
            )

        return _proxy_instance


def _normalise_arxiv_id(arxiv_id: str) -> str:
    value = arxiv_id.strip()
    if value.lower().startswith("arxiv:"):
        value = value.split(":", 1)[1].strip()
    return value


def _arxiv_pdf_blob_key(arxiv_id: str) -> str:
    safe = _normalise_arxiv_id(arxiv_id).replace("/", "_")
    return f"arxiv/papers/{safe}.pdf"


def _arxiv_markdown_blob_key(arxiv_id: str) -> str:
    safe = _normalise_arxiv_id(arxiv_id).replace("/", "_")
    return f"arxiv/papers/{safe}.md"


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

    # Common patterns from cache filenames.
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
