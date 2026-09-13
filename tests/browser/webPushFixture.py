"""Disposable localhost Web Push transport fixture; never uses a real database.

Run with ``pdm run python tests/browser/webPushFixture.py``. This serves the
candidate's actual notification controls, API and service worker. Synthetic
session binding and in-memory storage do NOT prove production authentication,
MongoDB durability, installed-app behaviour, or physical-device display.
"""

import base64
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["VON_DB_NAME"] = "test_web_push_fixture"

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, jsonify, render_template_string, session
import mongomock

from src.backend.services import web_push_service as push, message_service
from src.backend.server.routes import web_push_routes
from src.backend.security import access_control


def create_fixture():
    key = ec.generate_private_key(ec.SECP256R1())
    os.environ["VON_WEB_PUSH_PRIVATE_KEY"] = base64.urlsafe_b64encode(
        key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    ).decode()
    os.environ["VON_WEB_PUSH_PUBLIC_KEY"] = (
        base64.urlsafe_b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )
        .decode()
        .rstrip("=")
    )
    os.environ["VON_WEB_PUSH_SUBJECT"] = "mailto:push-fixture@example.org"
    os.environ[push.KEY_ENV] = Fernet.generate_key().decode()
    db = mongomock.MongoClient(tz_aware=True).db
    push.get_db = lambda: db
    push.get_concepts_collection = lambda: db.concepts
    push.scope_allowed = lambda actor, org: org == "#V#fixture_org"
    web_push_routes.scope = lambda: "#V#fixture_org"
    message_service.get_concepts_collection = lambda: db.concepts
    message_service.ConceptsRepository.insert_one = db.concepts.insert_one
    message_service.upsert_text_for_concept = lambda **kwargs: None
    message_service.maybe_launch_direct_message_workflow = lambda **kwargs: {
        "triggered": False
    }
    access_control.apply_concept_query_filter = lambda query: query
    static = ROOT / "src/frontend/web/von_interface/static"
    app = Flask(__name__, static_folder=str(static), static_url_path="/static")
    app.secret_key = secrets.token_hex(32)
    app.register_blueprint(web_push_routes.web_push_bp)
    source = (
        ROOT / "src/frontend/web/von_interface/templates/settings_tab.html"
    ).read_text()
    section = re.search(
        r'<section id="pushNotificationSettings".*?</section>', source, re.S
    ).group()

    @app.get("/von/")
    def home():
        session.update(
            user_concept_id="#V#fixture_alice",
            user_email="alice@example.test",
            auth_provider="browser_test_fixture",
        )
        return render_template_string(
            """<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="manifest" href="/von/manifest.webmanifest">
            <link rel="stylesheet" href="/static/styles.css"></head><body>
            <p>Disposable transport fixture — synthetic actor and in-memory messages.</p>
            <main class="settings-container">"""
            + section
            + """</main>
            <script type="module" src="/static/js/pushNotifications.js"></script></body></html>"""
        )

    @app.get("/health")
    def health():
        return jsonify(
            fixture="web-push-synthetic",
            source_commit=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            candidate_dirty=True,
            database="in-memory",
            models="disabled",
        )

    @app.post("/fixture/message")
    def create_message():
        # Fixed synthetic intent; never a free-form send surface.
        message = message_service.create_message(
            sender_id="#V#fixture_codex",
            recipient_ids=["#V#fixture_alice"],
            content="Synthetic coding-agent completion report.",
            org_id="#V#fixture_org",
        )
        return jsonify(message_id=message["concept_id"])

    push.start_worker()
    return app


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    create_fixture().run(host="127.0.0.1", port=5077, debug=False, use_reloader=False)
