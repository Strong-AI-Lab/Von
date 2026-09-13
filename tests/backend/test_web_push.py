"""Bounded notification delivery and the new actor/device authority boundary."""

import base64
from datetime import timedelta

import mongomock
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask

from src.backend.services import web_push_service as push
from src.backend.server.routes import web_push_routes as routes


@pytest.fixture
def store(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True).db
    monkeypatch.setattr(push, "get_db", lambda: db)
    monkeypatch.setattr(push, "get_concepts_collection", lambda: db.concepts)
    monkeypatch.setattr(
        push, "scope_allowed", lambda actor, org: org in (None, "#V#sail")
    )
    monkeypatch.setenv(push.KEY_ENV, Fernet.generate_key().decode())
    from src.backend.security import access_control

    monkeypatch.setattr(
        access_control, "apply_concept_query_filter", lambda query: query
    )
    sent = []
    monkeypatch.setattr(
        push,
        "send",
        lambda subscription, payload, **kwargs: sent.append(
            (subscription, payload, kwargs)
        )
        or "accepted",
    )
    return db, sent


@pytest.fixture
def subscription():
    key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/test-device",
        "keys": {
            "p256dh": base64.urlsafe_b64encode(key).decode().rstrip("="),
            "auth": base64.urlsafe_b64encode(b"a" * 16).decode().rstrip("="),
        },
    }


def enrol(store, subscription, *, actor="#V#alice", device="device", org="#V#sail"):
    db, sent = store
    result = push.subscribe(actor, device, org, subscription)
    challenge = sent[-1][1]["challenge"]
    assert push.confirm(actor, device, result["subscription_id"], challenge)
    return result["subscription_id"]


def message(
    db, identifier="message-one", recipient="#V#alice", org="#V#sail", stamp=None
):
    db.concepts.insert_one(
        {
            "concept_id": identifier,
            "created_at": stamp or push.now(),
            "relationships": {
                "is_an_instance_of": ["#V#direct_message"],
                "#V#has_sender": ["#V#codex_dgx"],
                "#V#has_recipient": [recipient],
            },
            "concept_data": {
                "organisation_concept_id": org,
                "content_fallback": "PRIVATE research report",
            },
        }
    )


def due(db):
    db[push.COLLECTION].update_many(
        {}, {"$set": {"retry_at": push.now() - timedelta(seconds=1)}}
    )


def test_challenge_proves_device_and_actor_without_returning_secret(
    store, subscription
):
    db, sent = store
    result = push.subscribe("#V#alice", "device", "#V#sail", subscription)
    identifier = result["subscription_id"]
    challenge = sent[-1][1]["challenge"]
    assert challenge not in str(result)
    assert subscription["endpoint"] not in str(db[push.COLLECTION].find_one())
    assert not push.confirm("#V#bob", "device", identifier, challenge)
    assert not push.confirm("#V#alice", "other-device", identifier, challenge)
    assert not push.confirm("#V#alice", "device", identifier, "wrong")
    assert push.confirm("#V#alice", "device", identifier, challenge)
    assert not push.confirm("#V#alice", "device", identifier, challenge)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://fcm.googleapis.com.evil.test/x",
        "https://push.apple.com.evil.test/x",
        "https://fcm.googleapis.com:444/x",
        "https://user:password@fcm.googleapis.com/x",
        "https://fcm.googleapis.com/x#fragment",
    ],
)
def test_endpoint_cannot_grant_arbitrary_network_access(subscription, endpoint):
    subscription["endpoint"] = endpoint
    with pytest.raises(ValueError):
        push.validate_subscription(subscription)


def test_endpoint_cannot_be_rebound_to_unrelated_actor_device_or_scope(
    store, subscription
):
    enrol(store, subscription)
    for actor, device, org in [
        ("#V#bob", "device", "#V#sail"),
        ("#V#alice", "another", "#V#sail"),
        ("#V#alice", "device", None),
    ]:
        with pytest.raises(PermissionError):
            push.subscribe(actor, device, org, subscription)


def test_durable_message_one_private_alert_and_late_insert_is_not_skipped(
    store, subscription
):
    db, sent = store
    enrol(store, subscription)
    early = push.now()
    message(db, "later")
    assert push.deliver_one()
    assert sent[-1][1]["kind"] == "message"
    assert "PRIVATE" not in str(sent[-1]) and "#V#alice" not in str(sent[-1])
    assert "later" not in str(sent[-1][1])
    # A create that began earlier can commit later than the newer message.
    message(db, "late-commit", stamp=early)
    due(db)
    assert push.deliver_one()
    due(db)
    push.deliver_one()
    assert len([item for item in sent if item[1]["kind"] == "message"]) == 2
    assert db[push.RECEIPTS].count_documents({"terminal": True}) == 2


def test_other_recipients_and_orgs_are_not_delivered(store, subscription):
    db, sent = store
    enrol(store, subscription)
    message(db, "bob-message", recipient="#V#bob")
    message(db, "other-org", org="#V#other")
    message(db, "personal", org=None)
    push.deliver_one()
    assert len(sent) == 1  # challenge only


def test_retry_is_bounded_and_uses_same_tag_and_receipt(
    store, subscription, monkeypatch
):
    db, sent = store
    enrol(store, subscription)
    message(db)
    attempts = []
    monkeypatch.setattr(
        push,
        "send",
        lambda sub, payload, **kw: attempts.append((payload, kw)) or "failed",
    )
    for _ in range(5):
        due(db)
        push.deliver_one()
    assert len(attempts) == 3
    assert all(attempt == attempts[0] for attempt in attempts)
    assert db[push.RECEIPTS].find_one()["terminal"]


def test_crash_after_provider_acceptance_keeps_receipt_and_retry_bound(
    store, subscription, monkeypatch
):
    db, sent = store
    enrol(store, subscription)
    message(db)
    attempts = []

    def crash(sub, payload, **kw):
        attempts.append(payload)
        raise RuntimeError("simulated process interruption after send")

    monkeypatch.setattr(push, "send", crash)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            push.deliver_one()
    push.deliver_one()
    assert len(attempts) == 3
    assert attempts[0] == attempts[1] == attempts[2]
    assert db[push.RECEIPTS].find_one()["terminal"]


def test_expiry_disable_and_membership_removal_stop_delivery(
    store, subscription, monkeypatch
):
    db, sent = store
    enrol(store, subscription)
    message(db)
    monkeypatch.setattr(push, "send", lambda *args, **kw: "expired")
    push.deliver_one()
    assert db[push.COLLECTION].count_documents({}) == 0
    monkeypatch.setattr(
        push,
        "send",
        lambda sub, payload, **kw: sent.append((sub, payload, kw)) or "accepted",
    )
    enrol(store, subscription)
    push.revoke_device("device")
    assert not push.deliver_one()
    enrol(store, subscription)
    monkeypatch.setattr(push, "scope_allowed", lambda *args: False)
    push.deliver_one()
    assert db[push.COLLECTION].count_documents({}) == 0


@pytest.fixture
def client(store, monkeypatch):
    app = Flask(__name__)
    app.secret_key = "fixture-key"
    app.register_blueprint(routes.web_push_bp)
    monkeypatch.setattr(
        push, "configuration", lambda: {"configured": True, "public_key": "public"}
    )
    monkeypatch.setattr(routes, "scope", lambda: "#V#sail")
    with app.test_client() as client:
        with client.session_transaction() as session:
            session.update(
                user_concept_id="#V#alice",
                user_email="alice@example.test",
                auth_provider="browser_test_fixture",
            )
        yield client


def test_session_and_origin_are_required_and_supplied_actor_is_ignored(
    client, store, subscription
):
    assert client.get("/api/notifications/status").status_code == 200
    path = "/api/notifications/subscribe"
    payload = {
        "subscription": subscription,
        "user_id": "#V#bob",
        "organisation": "#V#evil",
    }
    assert client.post(path, json=payload).status_code == 403
    assert (
        client.post(
            path, json=payload, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            path, json=payload, headers={"Origin": "http://localhost"}
        ).status_code
        == 200
    )
    row = store[0][push.COLLECTION].find_one()
    assert row["actor"] == "#V#alice" and row["organisation"] == "#V#sail"
    with client.session_transaction() as session:
        session.clear()
    assert (
        client.get(
            "/api/notifications/status", headers={"X-User-Concept-ID": "#V#alice"}
        ).status_code
        == 401
    )


def test_browser_account_change_revokes_old_subscription(client, store, subscription):
    client.get("/api/notifications/status")
    client.post(
        "/api/notifications/subscribe",
        json={"subscription": subscription},
        headers={"Origin": "http://localhost"},
    )
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#bob"
    result = client.get("/api/notifications/status")
    assert result.json["state"] == "disabled"
    assert store[0][push.COLLECTION].count_documents({}) == 0


def test_test_action_is_device_bound_and_rate_limited(store, subscription):
    identifier = enrol(store, subscription)
    with pytest.raises(ValueError):
        push.test_notification("#V#bob", "device", identifier)
    with pytest.raises(ValueError):
        push.test_notification("#V#alice", "other-device", identifier)
    assert (
        push.test_notification("#V#alice", "device", identifier)["provider_result"]
        == "accepted"
    )
    with pytest.raises(ValueError):
        push.test_notification("#V#alice", "device", identifier)


def test_navigation_rechecks_recipient_and_current_scope(
    client, store, subscription, monkeypatch
):
    db, _ = store
    enrol(store, subscription)
    message(db)
    push.deliver_one()
    receipt = db[push.RECEIPTS].find_one()
    from src.backend.services import message_service

    monkeypatch.setattr(message_service, "get_concepts_collection", lambda: db.concepts)
    url = "/api/notifications/open/" + receipt["_id"]
    assert client.get(url).json["last_message_id"] == "message-one"
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#bob"
    assert client.get(url).status_code == 404
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#alice"
    db.concepts.update_one({}, {"$set": {"concept_data.deleted": True}})
    assert client.get(url).status_code == 404


def test_canonical_coding_report_is_delivered_with_workflow_launch_suppressed(
    store, subscription, monkeypatch
):
    db, sent = store
    enrol(store, subscription)
    from src.backend.services import message_service

    monkeypatch.setattr(message_service, "get_concepts_collection", lambda: db.concepts)
    monkeypatch.setattr(
        message_service.ConceptsRepository, "insert_one", db.concepts.insert_one
    )
    monkeypatch.setattr(message_service, "upsert_text_for_concept", lambda **kw: None)
    monkeypatch.setattr(
        message_service,
        "maybe_launch_direct_message_workflow",
        lambda **kw: {"triggered": False, "reason": "suppressed"},
    )
    created = message_service.create_message(
        sender_id="#V#codex_dgx",
        recipient_ids=["#V#alice"],
        content="Completed the assigned coding work",
        org_id="#V#sail",
    )
    assert db.concepts.find_one({"concept_id": created["concept_id"]})
    assert push.deliver_one()
    assert sent[-1][1]["kind"] == "message"
    assert db[push.RECEIPTS].find_one()["message_id"] == created["concept_id"]


def test_queued_delivery_validation_is_bound_to_enrolment_and_logout(
    client, store, subscription
):
    db, sent = store
    client.get("/api/notifications/status")
    headers = {"Origin": "http://localhost"}
    result = client.post(
        "/api/notifications/subscribe",
        json={"subscription": subscription},
        headers=headers,
    )
    payload = sent[-1][1]
    assert (
        client.post(
            "/api/notifications/confirm", json=payload, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/notifications/validate", json=payload, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/notifications/validate",
            json={**payload, "generation": "old"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.post("/api/notifications/disable", json={}, headers=headers).status_code
        == 200
    )
    assert (
        client.post(
            "/api/notifications/validate", json=payload, headers=headers
        ).status_code
        == 403
    )


def test_logout_revokes_before_session_clear_and_reports_storage_failure(
    client, store, subscription, monkeypatch
):
    from src.backend.server.routes.auth_routes import auth_bp
    from src.backend.services import window_session_context_service

    client.application.register_blueprint(auth_bp, url_prefix="/von")
    monkeypatch.setattr(
        window_session_context_service,
        "delete_window_context_if_owned",
        lambda *args: None,
    )
    client.get("/api/notifications/status")
    client.post(
        "/api/notifications/subscribe",
        json={"subscription": subscription},
        headers={"Origin": "http://localhost"},
    )
    original = push.revoke_device
    monkeypatch.setattr(
        push,
        "revoke_device",
        lambda device: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    assert client.post("/von/api/auth/logout").status_code == 503
    with client.session_transaction() as session:
        assert session["user_concept_id"] == "#V#alice"
    monkeypatch.setattr(push, "revoke_device", original)
    assert client.post("/von/api/auth/logout").status_code == 200
    assert store[0][push.COLLECTION].count_documents({}) == 0
    with client.session_transaction() as session:
        assert "user_concept_id" not in session
