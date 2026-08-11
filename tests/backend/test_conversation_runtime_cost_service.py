from __future__ import annotations

from datetime import UTC, datetime

from flask import Flask


def _registry() -> dict:
    return {
        "models": [
            {
                "provider": "openai",
                "model_id": "gpt-test",
                "pricing": {
                    "schema_version": "llm_model_pricing.v1",
                    "version": "test-v1",
                    "source": "test",
                    "effective_at_utc": "2026-08-01T00:00:00Z",
                    "model_id": "gpt-test",
                    "currency": "USD",
                    "unit_tokens": 1_000,
                    "rates": {"input_tokens": 1.0, "output_tokens": 2.0},
                },
            }
        ]
    }


def _call(call_id: str, **overrides) -> dict:
    call = {
        "call_id": call_id,
        "provider": "openai",
        "effective_model": "gpt-test",
        "model_identity_source": "provider_response",
        "provider_request_sent": True,
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "completed_at_utc": "2026-08-10T12:00:00Z",
    }
    call.update(overrides)
    return call


def test_pure_snapshot_deduplicates_session_calls_and_marks_missing_runtime_timestamps():
    from src.backend.services.conversation_runtime_cost_service import (
        build_conversation_runtime_cost_snapshot,
    )

    snapshot = build_conversation_runtime_cost_snapshot(
        conversation_records=[
            {"request_id": "owner-turn", "llm_calls": [_call("owner-call")]},
            {
                "request_id": "invitee-turn",
                "llm_calls": [_call("owner-call"), _call("invitee-call")],
            },
        ],
        actor_runtime_records=[
            {"request_id": "owner-turn", "llm_calls": [_call("owner-call")]},
            {
                "request_id": "missing-time",
                "llm_calls": [_call("missing-time-call", completed_at_utc=None)],
            },
        ],
        conversation_session_id="shared-session",
        actor_concept_id="#V#owner",
        namespace="#V#owner@org",
        server_started_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        server_started_at_utc="2026-08-10T11:00:00Z",
        pid=42,
        model_registry=_registry(),
        as_of=datetime(2026, 8, 10, 12, 1, tzinfo=UTC),
    )

    assert snapshot["schema_version"] == "conversation_runtime_cost_snapshot.v1"
    assert snapshot["conversation"]["unique_call_count"] == 2
    assert snapshot["conversation"]["duplicate_call_count"] == 1
    assert snapshot["since_restart"]["estimated_cost"]["status"] == "partial"
    assert snapshot["since_restart"]["estimated_cost"]["amount"] is None
    assert snapshot["since_restart"]["estimated_cost"]["known_amount"] == 0.12
    assert snapshot["since_restart"]["runtime_coverage"] == {
        "status": "partial",
        "timestamp_eligible_call_count": 1,
        "missing_timestamp_call_count": 1,
    }


def test_non_billable_call_without_timestamp_does_not_degrade_runtime_cost_coverage():
    from src.backend.services.conversation_runtime_cost_service import (
        build_conversation_runtime_cost_snapshot,
    )

    non_billable_call = _call(
        "local-call",
        provider="Ollama",
        provider_request_sent=True,
        completed_at_utc=None,
    )
    snapshot = build_conversation_runtime_cost_snapshot(
        conversation_records=[{"llm_calls": [non_billable_call]}],
        actor_runtime_records=[{"llm_calls": [non_billable_call]}],
        conversation_session_id="local-session",
        actor_concept_id="#V#owner",
        namespace="#V#owner@org",
        server_started_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        server_started_at_utc="2026-08-10T11:00:00Z",
        pid=42,
        model_registry=_registry(),
    )

    assert snapshot["since_restart"]["estimated_cost"]["status"] == "not_applicable"
    assert snapshot["since_restart"]["runtime_coverage"] == {
        "status": "complete",
        "timestamp_eligible_call_count": 0,
        "missing_timestamp_call_count": 0,
    }


def test_runtime_snapshot_excludes_only_a_request_bound_to_the_selected_session(monkeypatch):
    from src.backend.services import conversation_runtime_cost_service as service

    projection = service.turn_execution_cost_projection()
    assert "llm_calls" not in projection
    assert projection["llm_calls.usage"] == 1
    assert "llm_calls.prompt" not in projection

    records = [
        {
            "request_id": "active-request",
            "session_id": "selected-session",
            "user_id": "#V#owner",
            "namespace": "#V#owner@org",
            "created_at_utc": "2026-08-10T12:00:00Z",
            "llm_calls": [_call("active-call")],
        },
        {
            "request_id": "invitee-request",
            "session_id": "selected-session",
            "user_id": "#V#invitee",
            "namespace": "#V#invitee@org",
            "created_at_utc": "2026-08-10T12:00:00Z",
            "llm_calls": [_call("invitee-call")],
        },
        {
            "request_id": "recovered-request",
            "session_id": "other-session",
            "user_id": "#V#owner",
            "namespace": "#V#owner@org",
            "created_at_utc": "2026-08-10T10:00:00Z",
            "updated_at_utc": "2026-08-10T12:00:00Z",
            "llm_calls": [_call("recovered-call")],
        },
    ]

    queries: list[dict] = []

    class Collection:
        def find_one(self, query, projection):
            record = next(
                (
                    record
                    for record in records
                    if all(record.get(key) == value for key, value in query.items())
                ),
                None,
            )
            if record is None:
                return None
            return {
                key: value
                for key, value in record.items()
                if projection.get(key) == 1
            }

        def find(self, query, _projection):
            queries.append(query)

            def matches(record):
                for key, value in query.items():
                    if key == "$or":
                        if not any(
                            record.get(field, "") >= clause["$gte"]
                            for alternative in value
                            for field, clause in alternative.items()
                        ):
                            return False
                    elif isinstance(value, dict) and "$ne" in value:
                        if record.get(key) == value["$ne"]:
                            return False
                    elif record.get(key) != value:
                        return False
                return True

            return [record for record in records if matches(record)]

    monkeypatch.setattr(service, "get_turn_execution_records_collection", Collection)
    snapshot = service.get_conversation_runtime_cost_snapshot(
        conversation_session_id="selected-session",
        actor_concept_id="#V#owner",
        actor_namespace="#V#owner@org",
        server_started_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        server_started_at_utc="2026-08-10T11:00:00Z",
        pid=42,
        model_registry=_registry(),
        exclude_request_id="active-request",
    )

    assert snapshot["conversation"]["unique_call_count"] == 1
    assert snapshot["since_restart"]["unique_call_count"] == 1
    assert {
        "session_id": "selected-session",
        "request_id": {"$ne": "active-request"},
    } in queries
    assert {
        "user_id": "#V#owner",
        "namespace": "#V#owner@org",
        "$or": [
            {"created_at_utc": {"$gte": "2026-08-10T11:00:00Z"}},
            {"updated_at_utc": {"$gte": "2026-08-10T11:00:00Z"}},
        ],
        "request_id": {"$ne": "active-request"},
    } in queries

    not_yet_persisted = service.get_conversation_runtime_cost_snapshot(
        conversation_session_id="selected-session",
        actor_concept_id="#V#owner",
        actor_namespace="#V#owner@org",
        server_started_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        server_started_at_utc="2026-08-10T11:00:00Z",
        pid=42,
        model_registry=_registry(),
        exclude_request_id="not-yet-persisted",
        observe_request_id="not-yet-persisted",
    )
    assert not_yet_persisted["handover"] == {
        "request_id": "not-yet-persisted",
        "observed": False,
    }
    observed = service.get_conversation_runtime_cost_snapshot(
        conversation_session_id="selected-session",
        actor_concept_id="#V#owner",
        actor_namespace="#V#owner@org",
        server_started_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        server_started_at_utc="2026-08-10T11:00:00Z",
        pid=42,
        model_registry=_registry(),
        observe_request_id="active-request",
    )
    assert observed["handover"] == {
        "request_id": "active-request",
        "observed": True,
    }

    try:
        service.get_conversation_runtime_cost_snapshot(
            conversation_session_id="selected-session",
            actor_concept_id="#V#owner",
            actor_namespace="#V#owner@org",
            server_started_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
            server_started_at_utc="2026-08-10T11:00:00Z",
            pid=42,
            model_registry=_registry(),
            exclude_request_id="invitee-request",
        )
    except ValueError as exc:
        assert str(exc) == "exclude_request_id_not_owned_by_actor"
    else:
        raise AssertionError("another actor's request must not be excluded")


def test_cost_route_uses_trusted_scope_and_requires_conversation_access(monkeypatch):
    from src.backend.server.routes import von_routes

    captured: dict = {}
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#actor",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#actor@trusted_org"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#actor", None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(von_routes, "get_model_registry_snapshot", _registry)
    monkeypatch.setattr(
        von_routes,
        "get_conversation_runtime_cost_snapshot",
        lambda **kwargs: captured.update(kwargs)
        or {"schema_version": "conversation_runtime_cost_snapshot.v1"},
    )

    app = Flask(__name__)
    app.secret_key = "test"
    app.config["SERVER_START_TIME"] = "2026-08-10T11:00:00Z"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/cost_summary",
        query_string={
            "session_id": "allowed-session",
            "namespace": "#V#attacker@other_org",
            "organisation_concept_id": "#V#other_org",
        },
    )

    assert response.status_code == 200
    assert captured["actor_concept_id"] == "#V#actor"
    assert captured["actor_namespace"] == "#V#actor@trusted_org"

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *_args, **_kwargs: False,
    )
    denied = app.test_client().get(
        "/von/history/cost_summary", query_string={"session_id": "forbidden"}
    )
    assert denied.status_code == 403


def test_runtime_cost_actor_time_indexes_are_created(monkeypatch):
    from src.backend.services import turn_execution_record_service as records

    created: list[tuple[list[tuple[str, int]], str]] = []

    class Collection:
        def list_indexes(self):
            return []

        def create_index(self, keys, *, name, **_kwargs):
            created.append((keys, name))

    monkeypatch.setattr(records, "_TURN_EXECUTION_INDEXES_READY", False)
    records._ensure_turn_execution_indexes(Collection())

    assert ([
        ("user_id", records.ASCENDING),
        ("namespace", records.ASCENDING),
        ("created_at_utc", records.DESCENDING),
    ], "user_namespace_created_desc") in created
    assert ([
        ("user_id", records.ASCENDING),
        ("namespace", records.ASCENDING),
        ("updated_at_utc", records.DESCENDING),
    ], "user_namespace_updated_desc") in created
