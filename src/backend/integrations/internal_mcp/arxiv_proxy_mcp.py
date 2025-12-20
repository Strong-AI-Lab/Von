"""arXiv MCP proxy using official MCP SDK client.

Manages subprocess communication with external arxiv-mcp-server using the MCP protocol.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, List

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
                    if result.content:
                        for item in result.content:
                            if isinstance(item, mcp_types.TextContent):
                                # Try to parse as JSON if it looks like structured data
                                text_data = item.text
                                if text_data.strip().startswith("{"):
                                    try:
                                        import json

                                        return json.loads(text_data)
                                    except json.JSONDecodeError:
                                        return {"text": text_data}
                                return {"text": text_data}
                            elif isinstance(item, mcp_types.ImageContent):
                                return {"image": item.data, "mimeType": item.mimeType}
                            elif isinstance(item, mcp_types.EmbeddedResource):
                                return {"resource": item.resource, "type": item.type}

                    return {}

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

        return await self._call_tool("download_paper", arguments)

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
            storage_path = workspace_root / "data" / "arxiv_papers"

            # Allow override via environment variable
            env_storage = os.environ.get("ARXIV_STORAGE_PATH")
            if env_storage:
                storage_path = Path(env_storage)

            config = ArxivProxyConfig(storage_path=storage_path)
            _proxy_instance = ArxivMCPProxy(config)
            logger.info(
                "%s Initialized arXiv proxy with storage: %s", _LOG_TAG, storage_path
            )

        return _proxy_instance
