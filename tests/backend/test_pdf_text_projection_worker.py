from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace


def test_worker_applies_named_memory_cpu_and_core_limits(monkeypatch):
    from src.backend.services import pdf_text_projection_worker as worker

    applied: list[tuple[int, tuple[int, int]]] = []
    fake_resource = SimpleNamespace(
        RLIMIT_AS=1,
        RLIMIT_CPU=2,
        RLIMIT_CORE=3,
        RLIM_INFINITY=-1,
        getrlimit=lambda resource_kind: (
            (999, 3) if resource_kind == 2 else (999, -1)
        ),
        setrlimit=lambda resource_kind, limits: applied.append(
            (resource_kind, limits)
        ),
    )
    signal_handlers = []
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(
        worker.signal,
        "signal",
        lambda signal_number, handler: signal_handlers.append(
            (signal_number, handler)
        ),
    )

    worker._apply_resource_limits(
        memory_limit_bytes=123,
        cpu_limit_seconds=4,
    )

    assert applied == [
        (fake_resource.RLIMIT_AS, (123, 123)),
        # A stricter inherited hard limit remains authoritative.
        (fake_resource.RLIMIT_CPU, (3, 3)),
        (fake_resource.RLIMIT_CORE, (0, 0)),
    ]
    assert len(signal_handlers) == 1


def test_worker_fails_closed_before_reading_or_importing_parser(monkeypatch):
    from src.backend.services import pdf_text_projection_worker as worker

    results = []
    monkeypatch.setattr(
        worker,
        "_parse_args",
        lambda: argparse.Namespace(
            memory_limit_bytes=123,
            cpu_limit_seconds=4,
            max_input_bytes=100,
            max_pages=10,
            stop_after_chars=100,
        ),
    )
    monkeypatch.setattr(
        worker,
        "_apply_resource_limits",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unsupported")),
    )
    monkeypatch.setattr(
        worker,
        "_extract_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("parser must not run without hard limits")
        ),
    )
    monkeypatch.setattr(worker, "_write_result", results.append)

    assert worker.main() == 78
    assert results == [
        {
            "status": "isolation_unsupported",
            "error": "pdf_parser_isolation_setup_failed",
        }
    ]
