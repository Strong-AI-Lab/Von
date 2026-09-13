"""Device-bound Web Push delivery from durable canonical direct messages.

This is a derived delivery store, not Vontology authority. Message creation never
depends on push or launches a model. Leased subscriptions and durable receipts
keep retries bounded, reusing the opaque notification tag/navigation ID.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric import ec
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_db, get_concepts_collection
from ..security.token_encryption import encrypt_json, decrypt_json, get_fernet

KEY_ENV = "VON_WEB_PUSH_ENCRYPTION_KEY"
COLLECTION = "web_push_subscriptions"
RECEIPTS = "web_push_receipts"
LEASE_SECONDS = 120  # One bounded provider request (10 s), with recovery headroom.
_log = logging.getLogger(__name__)
_worker_lock = threading.Lock()
_worker = None


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def collection(name=COLLECTION):
    db = get_db()
    if db is None:
        raise RuntimeError("Notification storage unavailable")
    return db[name]


def configuration():
    """Return public setup state without leaking keys or provider errors."""
    public = os.getenv("VON_WEB_PUSH_PUBLIC_KEY", "").strip()
    configured = all(
        os.getenv(key, "").strip()
        for key in (
            "VON_WEB_PUSH_PRIVATE_KEY",
            "VON_WEB_PUSH_PUBLIC_KEY",
            "VON_WEB_PUSH_SUBJECT",
            KEY_ENV,
        )
    )
    if configured:
        try:
            get_fernet(env_var=KEY_ENV)
            from pywebpush import webpush  # noqa: F401
        except Exception:
            configured = False
    return {"configured": configured, "public_key": public if configured else None}


def validate_subscription(value):
    """Constrain the server's outbound request to supported push providers.

    A browser-supplied URL is not arbitrary server-side fetch authority. Exact
    provider hosts/suffixes plus HTTPS and no redirects exclude local targets.
    Endpoint possession still needs the push challenge below.
    """
    if not isinstance(value, dict):
        raise ValueError("Invalid subscription")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError("Invalid subscription")
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    allowed = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com"} or (
        host.endswith(".push.apple.com") and host != ".push.apple.com"
    )
    if (
        not allowed
        or parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Unsupported push provider")
    keys = value.get("keys") or {}
    try:
        public = base64.urlsafe_b64decode(keys["p256dh"] + "===")
        auth = base64.urlsafe_b64decode(keys["auth"] + "===")
        if len(public) != 65 or len(auth) != 16:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except Exception as exc:
        raise ValueError("Invalid subscription keys") from exc
    return {"endpoint": endpoint, "keys": {k: keys[k] for k in ("p256dh", "auth")}}


class _ProviderSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def send(subscription, payload, *, topic):
    """Return provider acceptance only. Never log endpoint/key/response bodies."""
    from pywebpush import WebPushException, webpush

    try:
        with _ProviderSession() as http:
            response = webpush(
                subscription_info=subscription,
                data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=os.environ["VON_WEB_PUSH_PRIVATE_KEY"],
                vapid_claims={"sub": os.environ["VON_WEB_PUSH_SUBJECT"]},
                ttl=60,
                timeout=10,
                requests_session=http,
                headers={"Topic": topic[:32], "Urgency": "normal"},
            )
        return "accepted" if 200 <= response.status_code < 300 else "failed"
    except WebPushException as exc:
        code = exc.response.status_code if exc.response is not None else None
        return "expired" if code in (404, 410) else "failed"
    except Exception:
        return "failed"


def scope_allowed(actor, organisation):
    if not organisation:
        return True
    from .organisation_membership_service import get_user_memberships

    return any(
        row.get("organisation_concept_id") == organisation
        for row in get_user_memberships(actor).get("memberships", [])
    )


def revoke_device(device):
    if device:
        collection().delete_many({"device": device})


def status(actor, device, organisation):
    # Browser-wide identity changes revoke the old actor on this device. Other
    # devices and same-actor organisation opt-ins are independent.
    coll = collection()
    coll.delete_many({"device": device, "actor": {"$ne": actor}})
    row = coll.find_one(
        {
            "device": device,
            "actor": actor,
            "organisation": organisation,
            "expires_at": {"$gt": now()},
        }
    )
    return {
        "state": row["state"] if row else "disabled",
        "subscription_id": row["_id"] if row else None,
        "generation": row.get("generation") if row else None,
        "last_delivery": row.get("last_delivery") if row else None,
    }


def subscribe(actor, device, organisation, subscription):
    subscription = validate_subscription(subscription)
    if not scope_allowed(actor, organisation):
        raise PermissionError("Organisation access required")
    coll = collection()
    identifier = digest(subscription["endpoint"])
    device_row = coll.find_one({"device": device})
    if device_row and device_row["_id"] != identifier:
        # One PushManager subscription per app installation. Do not accumulate
        # stale endpoints or bypass the bounded confirmation action by rotating URLs.
        if (
            device_row.get("challenge_after", now()).replace(tzinfo=timezone.utc)
            > now()
        ):
            raise ValueError("Wait one minute before enabling another subscription")
        coll.delete_one({"_id": device_row["_id"], "device": device})
    existing = coll.find_one({"_id": identifier})
    if existing:
        if (existing["actor"], existing["device"], existing["organisation"]) != (
            actor,
            device,
            organisation,
        ):
            raise PermissionError(
                "Disable notifications in the original account or scope first"
            )
        if existing["state"] == "active":
            coll.update_one(
                {"_id": identifier, "state": "active"},
                {
                    "$set": {
                        "encrypted": encrypt_json(subscription, env_var=KEY_ENV),
                        "expires_at": now() + timedelta(days=30),
                    }
                },
            )
            return {"state": "active", "subscription_id": identifier}
        # Pending/failed challenges are deliberately rate limited too.
        if existing["retry_at"].replace(tzinfo=timezone.utc) > now():
            return {"state": existing["state"], "subscription_id": identifier}
        coll.delete_one({"_id": identifier, "state": existing["state"]})
    challenge = secrets.token_urlsafe(32)
    stamp = now()
    row = {
        "_id": identifier,
        "actor": actor,
        "device": device,
        "organisation": organisation,
        "state": "pending",
        "encrypted": encrypt_json(subscription, env_var=KEY_ENV),
        "challenge": digest(challenge),
        "challenge_until": stamp + timedelta(minutes=5),
        "challenge_after": stamp + timedelta(minutes=1),
        "expires_at": stamp + timedelta(days=30),
        "enabled_at": stamp,
        "generation": secrets.token_hex(16),
        "retry_at": stamp + timedelta(minutes=1),
        "lease_until": stamp,
    }
    try:
        coll.insert_one(row)
    except DuplicateKeyError as exc:
        raise ValueError("Subscription changed; refresh its status") from exc
    outcome = send(
        subscription,
        {
            "kind": "confirmation",
            "id": identifier,
            "generation": row["generation"],
            "challenge": challenge,
        },
        topic=identifier,
    )
    coll.update_one({"_id": identifier}, {"$set": {"last_delivery": outcome}})
    return {
        "state": "pending",
        "subscription_id": identifier,
        "provider_result": outcome,
    }


def confirm(actor, device, identifier, challenge):
    result = collection().update_one(
        {
            "_id": identifier,
            "actor": actor,
            "device": device,
            "state": "pending",
            "challenge": digest(challenge),
            "challenge_until": {"$gt": now()},
        },
        {
            "$set": {"state": "active", "retry_at": now()},
            "$unset": {"challenge": "", "challenge_until": ""},
        },
    )
    return bool(result.modified_count)


def test_notification(actor, device, identifier):
    stamp = now()
    row = collection().find_one_and_update(
        {
            "_id": identifier,
            "actor": actor,
            "device": device,
            "state": "active",
            "expires_at": {"$gt": stamp},
            "$or": [
                {"test_after": {"$exists": False}},
                {"test_after": {"$lte": stamp}},
            ],
        },
        {"$set": {"test_after": stamp + timedelta(minutes=1)}},
    )
    if not row or not scope_allowed(actor, row["organisation"]):
        raise ValueError(
            "Enable notifications first; tests are limited to once a minute"
        )
    outcome = send(
        decrypt_json(row["encrypted"], env_var=KEY_ENV),
        {"kind": "test", "id": identifier, "generation": row["generation"]},
        topic=identifier,
    )
    if outcome == "expired":
        collection().delete_one({"_id": identifier})
    return {"provider_result": outcome}


def _next_message(row):
    from ..security.access_control import (
        override_current_actor,
        apply_concept_query_filter,
    )
    from .message_service import MESSAGE_TYPE_CONCEPT_ID, PREDICATE_RECIPIENT

    query = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        f"relationships.{PREDICATE_RECIPIENT}": row["actor"],
        "concept_data.organisation_concept_id": row["organisation"],
        "concept_data.deleted": {"$ne": True},
        "created_at": {
            "$gte": max(
                row["enabled_at"].replace(tzinfo=timezone.utc),
                now() - timedelta(days=1),
            )
        },
    }
    with override_current_actor(row["actor"], row["organisation"]):
        # Anti-join terminal receipts instead of trusting a wall-clock cursor:
        # a concurrent canonical insert may finish after a newer message.
        messages = list(
            get_concepts_collection().aggregate(
                [
                    {"$match": apply_concept_query_filter(query)},
                    {"$sort": {"created_at": 1, "concept_id": 1}},
                    {
                        "$lookup": {
                            "from": RECEIPTS,
                            "localField": "concept_id",
                            "foreignField": "message_id",
                            "as": "push_receipts",
                        }
                    },
                    {
                        "$match": {
                            "push_receipts": {
                                "$not": {
                                    "$elemMatch": {
                                        "generation": row["generation"],
                                        "terminal": True,
                                    }
                                }
                            }
                        }
                    },
                    {"$limit": 1},
                    {"$project": {"push_receipts": 0}},
                ]
            )
        )
        return messages[0] if messages else None


def deliver_one():
    """One durable leased delivery; multiple web processes may safely compete."""
    stamp = now()
    lease = secrets.token_hex(16)
    coll = collection()
    row = coll.find_one_and_update(
        {
            "state": "active",
            "expires_at": {"$gt": stamp},
            "retry_at": {"$lte": stamp},
            "lease_until": {"$lte": stamp},
        },
        {
            "$set": {
                "lease": lease,
                "lease_until": stamp + timedelta(seconds=LEASE_SECONDS),
            }
        },
        sort=[("retry_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not row:
        return False
    owned = {"_id": row["_id"], "lease": lease, "state": "active"}
    try:
        if not scope_allowed(row["actor"], row["organisation"]):
            coll.delete_one(owned)
            return True
        message = _next_message(row)
        if not message:
            coll.update_one(
                owned, {"$set": {"retry_at": stamp + timedelta(seconds=30)}}
            )
            return True
        # Deterministic delivery identity survives process death and provider
        # acceptance followed by a lost response. It contains no actor/message ID.
        receipt_id = digest(row["generation"] + message["concept_id"])
        receipts = collection(RECEIPTS)
        receipts.update_one(
            {"_id": receipt_id},
            {
                "$setOnInsert": {
                    "actor": row["actor"],
                    "organisation": row["organisation"],
                    "message_id": message["concept_id"],
                    "subscription_id": row["_id"],
                    "generation": row["generation"],
                    "terminal": False,
                    "attempts": 0,
                    "expires_at": stamp + timedelta(days=7),
                }
            },
            upsert=True,
        )
        # Count before I/O so crashes cannot cause unlimited retries.
        if not coll.find_one(owned):
            return True
        claimed = receipts.find_one_and_update(
            {"_id": receipt_id},
            {"$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if claimed["attempts"] > 3:
            outcome = "failed"
        elif row["actor"] in message.get("concept_data", {}).get(
            "read_by", []
        ) or message["created_at"].replace(tzinfo=timezone.utc) < stamp - timedelta(
            days=1
        ):
            outcome = "skipped"
        else:
            outcome = send(
                decrypt_json(row["encrypted"], env_var=KEY_ENV),
                {
                    "kind": "message",
                    "id": row["_id"],
                    "generation": row["generation"],
                    "receipt": receipt_id,
                },
                topic=receipt_id,
            )
        receipts.update_one(
            {"_id": receipt_id},
            {
                "$set": {
                    "provider_result": outcome,
                    "attempts": claimed["attempts"],
                    "updated_at": stamp,
                    "terminal": outcome != "failed" or claimed["attempts"] >= 3,
                }
            },
        )
        if outcome == "expired":
            coll.delete_one(owned)
        elif outcome != "failed" or claimed["attempts"] >= 3:
            coll.update_one(
                owned,
                {
                    "$set": {
                        "last_delivery": outcome,
                        "retry_at": stamp,
                    }
                },
            )
        else:
            coll.update_one(
                owned,
                {
                    "$set": {
                        "last_delivery": outcome,
                        "retry_at": stamp + timedelta(minutes=claimed["attempts"]),
                    }
                },
            )
        return True
    finally:
        coll.update_one(
            owned, {"$set": {"lease_until": now()}, "$unset": {"lease": ""}}
        )


def start_worker():
    """Independent of model/workflow launch suppression; explicitly configured."""
    global _worker
    if not configuration()["configured"]:
        return
    with _worker_lock:
        if _worker and _worker.is_alive():
            return

        def run():
            while True:
                try:
                    for _ in range(100):
                        if not deliver_one():
                            break
                except Exception:
                    _log.warning(
                        "Web Push delivery unavailable; canonical messages retained"
                    )
                threading.Event().wait(30)

        # No data rewrite/migration. These indexes bound routine device lookup
        # and delivery scans; expired capability/navigation records self-delete.
        collection().create_index("device", unique=True)
        collection().create_index([("state", 1), ("retry_at", 1), ("lease_until", 1)])
        collection(RECEIPTS).create_index("message_id")
        get_concepts_collection().create_index(
            [
                ("relationships.#V#has_recipient", 1),
                ("concept_data.organisation_concept_id", 1),
                ("created_at", 1),
            ],
            name="direct_message_push_recipient_org_created",
        )
        for name in (COLLECTION, RECEIPTS):
            collection(name).create_index("expires_at", expireAfterSeconds=0)
        _worker = threading.Thread(target=run, name="web-push-delivery", daemon=True)
        _worker.start()
