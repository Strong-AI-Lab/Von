"""Resource-confined subprocess worker for untrusted PDF text projection.

This file is invoked as an isolated Python script.  It intentionally imports
PyMuPDF only after POSIX address-space and CPU limits have been installed.  On
platforms where those limits cannot be established it returns an explicit
unsupported result without opening the PDF.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from typing import Any

PROTOCOL_VERSION = "pdf_text_projection_worker.v1"


class _WorkerResourceLimit(Exception):
    """A worker resource boundary was reached while parsing."""


def _write_result(payload: dict[str, Any]) -> None:
    payload = {"schema_version": PROTOCOL_VERSION, **payload}
    sys.stdout.buffer.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    sys.stdout.buffer.flush()


def _apply_resource_limits(*, memory_limit_bytes: int, cpu_limit_seconds: int) -> None:
    try:
        import resource
    except ImportError as exc:  # pragma: no cover - exercised on non-POSIX hosts
        raise RuntimeError("posix_resource_limits_unavailable") from exc

    if not hasattr(resource, "RLIMIT_AS") or not hasattr(resource, "RLIMIT_CPU"):
        raise RuntimeError("required_resource_limits_unavailable")

    def _at_most_inherited_hard_limit(
        resource_kind: int,
        *,
        requested_soft: int,
        requested_hard: int,
    ) -> tuple[int, int]:
        _inherited_soft, inherited_hard = resource.getrlimit(resource_kind)
        if inherited_hard != resource.RLIM_INFINITY:
            requested_hard = min(requested_hard, inherited_hard)
            requested_soft = min(requested_soft, requested_hard)
        return requested_soft, requested_hard

    resource.setrlimit(
        resource.RLIMIT_AS,
        _at_most_inherited_hard_limit(
            resource.RLIMIT_AS,
            requested_soft=memory_limit_bytes,
            requested_hard=memory_limit_bytes,
        ),
    )
    resource.setrlimit(
        resource.RLIMIT_CPU,
        _at_most_inherited_hard_limit(
            resource.RLIMIT_CPU,
            requested_soft=cpu_limit_seconds,
            requested_hard=cpu_limit_seconds + 1,
        ),
    )
    if hasattr(resource, "RLIMIT_CORE"):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    if hasattr(signal, "SIGXCPU"):

        def _cpu_limit_reached(_signum, _frame):
            raise _WorkerResourceLimit("pdf_parser_cpu_limit_exceeded")

        signal.signal(signal.SIGXCPU, _cpu_limit_reached)


def _append_bounded_text(
    parts: list[str],
    value: str,
    *,
    length: int,
    stop_after_chars: int,
) -> tuple[int, bool]:
    remaining = max(0, stop_after_chars - length)
    if remaining:
        parts.append(value[:remaining])
    new_length = length + len(value)
    return min(new_length, stop_after_chars), new_length >= stop_after_chars


def _extract_pdf(
    data: bytes,
    *,
    max_pages: int,
    stop_after_chars: int,
) -> dict[str, Any]:
    import fitz  # type: ignore[import-not-found]

    parts: list[str] = []
    length = 0
    resource_truncated = False
    with fitz.open(stream=data, filetype="pdf") as document:
        page_count = int(document.page_count)
        for page_number in range(min(page_count, max_pages)):
            page = document.load_page(page_number)
            # PyMuPDF may materialise a large decompressed text layer here. It
            # is intentionally inside the child process after RLIMIT_AS/CPU.
            page_text = str(page.get_text("text"))
            if page_text:
                length, char_limit_reached = _append_bounded_text(
                    parts,
                    page_text,
                    length=length,
                    stop_after_chars=stop_after_chars,
                )
                if char_limit_reached:
                    resource_truncated = True
                    break
            if length >= stop_after_chars:
                break
        if page_count > max_pages:
            resource_truncated = True

    text = "\n".join(parts).strip()
    if text:
        return {
            "status": "ok",
            "text": text,
            "method": "pymupdf",
            "error": (
                "pdf_projection_limit_reached" if resource_truncated else None
            ),
            "resource_truncated": resource_truncated,
        }
    if page_count > max_pages:
        return {
            "status": "ok",
            "text": None,
            "method": "pdf_text_layer_empty",
            "error": "pdf_page_limit_reached",
            "resource_truncated": True,
        }
    return {
        "status": "ok",
        "text": None,
        "method": "pdf_text_layer_empty",
        "error": None,
        "resource_truncated": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--memory-limit-bytes", required=True, type=int)
    parser.add_argument("--cpu-limit-seconds", required=True, type=int)
    parser.add_argument("--max-input-bytes", required=True, type=int)
    parser.add_argument("--max-pages", required=True, type=int)
    parser.add_argument("--stop-after-chars", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        _apply_resource_limits(
            memory_limit_bytes=args.memory_limit_bytes,
            cpu_limit_seconds=args.cpu_limit_seconds,
        )
    except Exception:  # noqa: BLE001 - fail closed before parser import
        _write_result(
            {
                "status": "isolation_unsupported",
                "error": "pdf_parser_isolation_setup_failed",
            }
        )
        return 78

    try:
        data = sys.stdin.buffer.read(args.max_input_bytes + 1)
        if len(data) > args.max_input_bytes:
            raise _WorkerResourceLimit("pdf_parser_input_limit_exceeded")
        result = _extract_pdf(
            data,
            max_pages=args.max_pages,
            stop_after_chars=args.stop_after_chars,
        )
    except _WorkerResourceLimit as exc:
        result = {
            "status": "resource_limit",
            "error": str(exc),
        }
    except MemoryError:
        result = {
            "status": "resource_limit",
            "error": "pdf_parser_memory_limit_exceeded",
        }
    except Exception as exc:  # noqa: BLE001 - third-party parser boundary
        result = {
            "status": "parser_error",
            "error": type(exc).__name__,
        }

    try:
        _write_result(result)
    except (MemoryError, OSError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
