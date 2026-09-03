from __future__ import annotations

import ast
import logging
from io import StringIO
from pathlib import Path


def _suppressed_third_party_logger_names() -> set[str]:
    source_path = (
        Path(__file__).resolve().parents[2] / "src" / "workflows" / "von" / "main.py"
    )
    module = ast.parse(source_path.read_text(encoding="utf-8"))

    for node in module.body:
        if not isinstance(node, ast.For):
            continue
        if not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "_noisy_logger_name":
            continue
        if not isinstance(node.iter, (ast.Tuple, ast.List)):
            raise TypeError("suppressed logger names must remain a literal sequence")

        names: set[str] = set()
        for item in node.iter.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                raise TypeError("suppressed logger names must remain string literals")
            names.add(item.value)
        return names

    raise AssertionError("Could not find the third-party logger suppression loop")


def test_von_main_suppresses_oauth_dependency_debug_logging() -> None:
    names = _suppressed_third_party_logger_names()

    assert {"requests_oauthlib", "oauthlib"}.issubset(names)


def test_oauth_suppression_keeps_warnings_and_first_party_debug_logging() -> None:
    names = _suppressed_third_party_logger_names()
    oauth_parents = [
        logging.getLogger(name) for name in ("requests_oauthlib", "oauthlib")
    ]
    oauth_children = [
        logging.getLogger("requests_oauthlib.oauth2_session"),
        logging.getLogger("oauthlib.oauth2.rfc6749.parameters"),
    ]
    first_party = logging.getLogger("src.backend.auth_service.logging_test")
    tracked_loggers = [*oauth_parents, *oauth_children, first_party]
    original_root_level = logging.getLogger().level
    original_logger_state = {
        logger: (logger.level, logger.disabled, logger.propagate)
        for logger in tracked_loggers
    }
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root_logger = logging.getLogger()

    try:
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        for logger_name in ("requests_oauthlib", "oauthlib"):
            assert logger_name in names
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        for logger in [*oauth_children, first_party]:
            logger.setLevel(logging.NOTSET)
            logger.disabled = False
            logger.propagate = True

        oauth_children[0].debug("synthetic callback code and client secret")
        oauth_children[1].debug("synthetic access and refresh tokens")
        first_party.debug("first-party-debug-remains-visible")
        oauth_children[0].warning("oauth-warning-remains-visible")
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(original_root_level)
        for logger, (level, disabled, propagate) in original_logger_state.items():
            logger.setLevel(level)
            logger.disabled = disabled
            logger.propagate = propagate

    output = stream.getvalue()
    assert "synthetic callback code and client secret" not in output
    assert "synthetic access and refresh tokens" not in output
    assert "first-party-debug-remains-visible" in output
    assert "oauth-warning-remains-visible" in output
