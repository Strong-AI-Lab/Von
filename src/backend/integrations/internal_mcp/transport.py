"""Transport utilities for the internal MCP gateway."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransportResult:
    """Container for transport execution metadata."""

    payload: Any
    duration_ms: float


class InternalMCPTransport:
    """Execute registered handlers with lightweight timing and logging."""

    def __init__(self, *, read_timeout_sec: float = 6.0, write_timeout_sec: float = 20.0):
        self._read_timeout_sec = float(read_timeout_sec)
        self._write_timeout_sec = float(write_timeout_sec)

    @property
    def read_timeout_sec(self) -> float:
        return self._read_timeout_sec

    @property
    def write_timeout_sec(self) -> float:
        return self._write_timeout_sec

    def execute(self, *, method_name: str, handler: Callable[..., Any], payload: Dict[str, Any], timeout_sec: float | None, log_tag: str = "[mcp_gateway]") -> TransportResult:
        """Execute handler and capture timing.

        Timeout enforcement is deferred to future iterations when handlers are
        executed in isolated workers. For now we log the requested timeout to
        ensure observability while keeping the transport synchronous.
        """

        start = time.perf_counter()
        logger.info("%s invoking %s (timeout=%.1fs)", log_tag, method_name, timeout_sec if timeout_sec else 0.0)
        result = handler(**payload)
        duration_ms = (time.perf_counter() - start) * 1000.0
        logger.info("%s completed %s in %.2fms", log_tag, method_name, duration_ms)
        return TransportResult(payload=result, duration_ms=duration_ms)
