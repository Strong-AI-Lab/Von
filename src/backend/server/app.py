"""Compatibility shim exposing a Flask `app` instance for tests expecting `src.backend.server.app`.

Historically tests imported `app` directly. The refactor introduced `create_flask_app` in
`utils_flask.py`. This module now instantiates an app using lightweight dummy dependency
functions so existing tests (e.g. salient predicate header mode) can import a ready
Flask application without modifying the test code.

If a richer initialization is required later (e.g. real model listing/generation),
those functions can be updated or injected via a different entrypoint.
"""
from __future__ import annotations

from typing import List, Dict, Optional

from .utils_flask import create_flask_app
from flask import current_app
from .routes.vontology_routes import list_salient_predicates_for_instance  # type: ignore

# Lightweight dummy dependency functions

def _list_models() -> List[str]:
    return ["dummy-model"]


def _generate(prompt: str, context: Optional[List[Dict[str, str]]], model: Optional[str]) -> str:
    return f"[dummy-response model={model or 'dummy-model'} prompt={prompt[:40]}]"

# Expose the app object expected by legacy imports
app = create_flask_app(_list_models, _generate)

# Legacy alias: earlier tests call /api/vontology/predicates/salient but blueprint now mounted at /vontology/api/vontology
# Provide a direct route bridging to the blueprint handler if not already registered.
if '/api/vontology/predicates/salient' not in [r.rule for r in app.url_map.iter_rules()]:
    @app.route('/api/vontology/predicates/salient', methods=['GET'])
    def _legacy_salient_alias():  # pragma: no cover - simple delegation
        return list_salient_predicates_for_instance()
