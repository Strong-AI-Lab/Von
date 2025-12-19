import asyncio
import time

from src.backend.mcp_server.mcp_stdio_server import _run_blocking_with_timeout, VonChatRunTimeout


def test_run_blocking_with_timeout_times_out():
    async def _runner():
        def _block():
            time.sleep(0.2)

        try:
            await _run_blocking_with_timeout(_block, timeout_seconds=0.05)
        except VonChatRunTimeout as exc:
            assert exc.pid > 0
            # Thread id may be None if timeout happens before worker starts.
            assert exc.thread_id is None or exc.thread_id > 0
            return True
        return False

    assert asyncio.run(_runner()) is True
