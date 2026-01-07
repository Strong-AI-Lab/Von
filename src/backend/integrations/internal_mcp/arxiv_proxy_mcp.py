"""arXiv MCP proxy using official MCP SDK client.

Manages subprocess communication with external arxiv-mcp-server using the MCP protocol.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, List

from src.backend.services.blob_store import get_blob_store_from_env

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

        result = await self._call_tool("download_paper", arguments)
        return self._store_downloaded_pdf(result=result, arxiv_id=arxiv_id)

    def _store_downloaded_pdf(self, *, result: Any, arxiv_id: str) -> Dict[str, Any]:
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
                raise ArxivProxyError(
                    "arXiv download succeeded but no file path was returned by arxiv-mcp-server"
                )

        path = Path(file_path)
        if not path.is_absolute():
            path = self._config.storage_path / path

        if not path.exists():
            raise ArxivProxyError(f"Downloaded PDF not found at: {path}")

        blob_store = get_blob_store_from_env()
        storage_key = _arxiv_pdf_blob_key(arxiv_id)

        try:
            ref = blob_store.put_bytes(
                storage_key,
                path.read_bytes(),
                content_type="application/pdf",
                metadata={
                    "source": "arxiv",
                    "arxiv_id": _normalise_arxiv_id(arxiv_id),
                    "original_path": str(path),
                },
            )
        except Exception as exc:
            raise ArxivProxyError(f"Failed to store PDF in blob store: {exc}") from exc

        stored = dict(result)
        stored["arxiv_id"] = arxiv_id
        stored["file_path"] = str(path)
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
        return await self._call_tool("list_papers", {})

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


# Singleton instance
_proxy_instance: Optional[ArxivMCPProxy] = None
_proxy_lock = asyncio.Lock()


async def get_arxiv_proxy() -> ArxivMCPProxy:
    """Get or create the global arXiv proxy instance."""
    global _proxy_instance

    async with _proxy_lock:
        if _proxy_instance is None:
            # Determine storage path
            import os

            workspace_root = Path(__file__).parent.parent.parent.parent.parent
            # Cache directory for external arxiv-mcp-server. Durable storage is the blob store.
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
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            path = f"//{parsed.netloc}{path}"
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
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

    return None


def _extract_download_file_path(result: Dict[str, Any]) -> str | None:
    keys = {"file_path", "path", "filepath", "filename", "uri", "text"}
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
            for key, value in mapping.items():
                if key in keys and isinstance(value, str):
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
