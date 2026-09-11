from datetime import UTC, datetime, timedelta

import mongomock
import pytest
from flask import Flask

from src.backend.services import conversation_catalogue_service as catalogue
from src.backend.services import conversation_read_service as reads


@pytest.fixture
def data(monkeypatch):
    db = mongomock.MongoClient().db
    monkeypatch.setattr(
        catalogue.chats,
        "get_chat_history_collection_service",
        lambda **kwargs: db.history,
    )
    monkeypatch.setattr(
        catalogue.chats, "backfill_conversation_activity_metadata", lambda *a, **k: None
    )
    monkeypatch.setattr(
        catalogue, "list_accepted_invites_for_user", lambda **kwargs: []
    )
    monkeypatch.setattr(
        catalogue,
        "apply_conversation_preferences",
        lambda **kwargs: kwargs["conversations"],
    )
    monkeypatch.setattr(catalogue, "project_unread", lambda actor, rows: rows)
    monkeypatch.setattr(
        reads, "get_application_settings_collection", lambda: db.settings
    )
    monkeypatch.setattr(
        reads.settings_service,
        "get_settings_batch",
        lambda keys: {
            r["setting_name"]: r["value"]
            for r in db.settings.find({"setting_name": {"$in": keys}})
        },
    )
    monkeypatch.setattr(
        reads.settings_service,
        "get_setting",
        lambda key: (db.settings.find_one({"setting_name": key}) or {}).get("value"),
    )
    return db


def test_unconsumed_candidates_and_equal_times_page_without_omission(data, monkeypatch):
    stamp = datetime(2026, 9, 11, tzinfo=UTC)
    for i in range(205):
        data.history.insert_one(
            {
                "user_id": "#V#alice",
                "namespace": "alice@lab",
                "session_id": f"s{i:03}",
                "last_contribution_at": stamp - timedelta(seconds=i // 2),
                "created_at": stamp,
            }
        )
    # Two message exchanges competing with a much deeper ordinary history.
    messages = [
        {
            "session_id": f"messages:{i}",
            "catalogue_sort_key": f"message:{i}",
            "last_message_at": (stamp - timedelta(seconds=i)).isoformat(),
            "source_kind": "message_exchange",
        }
        for i in (1, 20)
    ]

    def message_page(actor, **kwargs):
        before = kwargs["before"]
        remaining = [
            r
            for r in messages
            if not before
            or r["last_message_at"] < before["timestamp"]
            or (
                r["last_message_at"] == before["timestamp"]
                and r["catalogue_sort_key"] > before["key"]
            )
        ]
        return {
            "conversations": remaining[: kwargs["limit"]],
            "has_more": len(remaining) > kwargs["limit"],
        }

    monkeypatch.setattr(catalogue.messages, "list_exchanges", message_page)
    app = Flask(__name__)
    app.secret_key = "catalogue-test-only"
    seen, cursor = [], None
    with app.app_context():
        for _ in range(30):
            page = catalogue.list_catalogue(
                "#V#alice", namespace="alice@lab", limit=10, cursor=cursor
            )
            assert page["coverage_complete"]
            seen.extend(row["session_id"] for row in page["conversations"])
            cursor = page["next_cursor"]
            if not cursor:
                break
    assert len(seen) == len(set(seen)) == 207
    assert seen[:6] == ["s000", "s001", "s002", "s003", "messages:1", "s004"]


def test_owner_and_namespace_constraints_apply_before_paging(data, monkeypatch):
    for actor, namespace, sid in [
        ("#V#alice", "alice@lab", "mine"),
        ("#V#eve", "alice@lab", "private"),
        ("#V#alice", "alice@other", "elsewhere"),
    ]:
        data.history.insert_one(
            {
                "user_id": actor,
                "namespace": namespace,
                "session_id": sid,
                "last_contribution_at": datetime.now(UTC),
            }
        )
    monkeypatch.setattr(
        catalogue.messages,
        "list_exchanges",
        lambda *a, **k: {"conversations": [], "has_more": False},
    )
    page = catalogue.list_catalogue("#V#alice", namespace="alice@lab")
    assert [r["session_id"] for r in page["conversations"]] == ["mine"]


def test_failed_source_remains_explicit_and_does_not_advance_past_missing_rows(
    data, monkeypatch
):
    monkeypatch.setattr(
        catalogue,
        "_chat_page",
        lambda *a: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        catalogue.messages,
        "list_exchanges",
        lambda *a, **k: {
            "conversations": [{"session_id": "m", "catalogue_sort_key": "message:m"}],
            "has_more": True,
        },
    )
    page = catalogue.list_catalogue("#V#alice", namespace="alice@lab")
    assert not page["coverage_complete"] and page["coverage"]["messages"]
    assert page["conversations"][0]["session_id"] == "m" and page["next_cursor"] is None


def test_read_baseline_is_quiet_then_incoming_activity_is_per_viewer_and_monotonic(
    data,
):
    old = datetime.now(UTC) - timedelta(days=1)
    rows = [{"session_id": "s", "last_incoming_contribution_at": old}]
    assert reads.project_unread("alice", rows)[0]["shared_unread_count"] == 0
    baseline = reads.settings_service.get_setting(reads._key("alice", "__baseline__"))
    fresh = datetime.now(UTC) + timedelta(seconds=1)
    rows[0]["last_incoming_contribution_at"] = fresh
    assert reads.project_unread("alice", rows)[0]["shared_unread_count"] == 1
    reads.acknowledge("alice", "s", fresh)
    reads.acknowledge("alice", "s", old)
    assert reads.project_unread("alice", rows)[0]["shared_unread_count"] == 0
    assert (
        reads.settings_service.get_setting(reads._key("alice", "__baseline__"))
        == baseline
    )
    assert reads.settings_service.get_setting(reads._key("bob", "s")) is None


def test_shared_receipts_are_not_reset_by_normal_conversation_projection(data):
    rows = [{"session_id": "shared", "shared_with_me": True, "shared_unread_count": 3}]
    assert reads.project_unread("alice", rows)[0]["shared_unread_count"] == 3
