from __future__ import annotations

from flask import Flask


def test_settings_cost_summary_is_bound_to_ambient_actor_scope(monkeypatch) -> None:
    from src.backend.server.routes import settings_routes

    captured: dict = {}

    def fake_summary(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": "llm_model_turn_cost_summary.v1",
            "status": "insufficient_coverage",
            "model_identity": {"provider": "openai", "model": "gpt-test"},
            "average_attributed_cost_per_turn": {
                "amount": None,
                "currency": None,
            },
            "sample_window": {"attributed_turn_count": 0},
            "coverage": {
                "priced_turn_count": 0,
                "partial_turn_count": 0,
                "unavailable_turn_count": 0,
                "attributed_turn_count": 0,
            },
            "pricing": None,
        }

    monkeypatch.setattr(
        settings_routes, "get_effective_user_concept_id", lambda: "#V#person"
    )
    monkeypatch.setattr(
        settings_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#person@org"},
    )
    monkeypatch.setattr(settings_routes, "get_model_registry_snapshot", dict)
    monkeypatch.setattr(
        settings_routes, "get_actor_historical_model_cost_summary", fake_summary
    )

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(settings_routes.settings_bp, url_prefix="/api/settings")
    response = app.test_client().get(
        "/api/settings/llm/cost_summary",
        query_string={"provider": "openai", "model": "gpt-test"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "insufficient_coverage"
    assert captured["user_id"] == "#V#person"
    assert captured["namespace"] == "#V#person@org"
    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-test"
    assert captured["limit"] == 200
    assert captured["window_days"] == 30


def test_historical_summary_reprices_stored_usage_once_without_duplicate_sources() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    stored_call = {
        "call_id": "call-1",
        "provider": "openai",
        "effective_model": "gpt-test",
        "model_identity_source": "provider_response",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "estimated_cost": {
            "status": "estimated",
            "amount": 0.25,
            "known_amount": 0.25,
            "currency": "USD",
            "pricing": {
                "source": "test",
                "version": "v1",
                "effective_at_utc": "2026-08-05T00:00:00Z",
            },
        },
    }
    result = build_historical_model_cost_summary(
        [
            {
                "created_at_utc": "2026-08-05T10:00:00Z",
                "llm_usage_cost_summary": {"status": "legacy-projection"},
                "llm_calls": [stored_call],
                "execution": {"llm_calls": [stored_call]},
            }
        ],
        provider="openai",
        model="gpt-test",
        model_registry={
            "models": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "pricing": {
                        "schema_version": "llm_model_pricing.v1",
                        "version": "current-v2",
                        "source": "current-test-pricing",
                        "effective_at_utc": "2026-08-05T00:00:00Z",
                        "model_id": "gpt-test",
                        "currency": "USD",
                        "unit_tokens": 1_000,
                        "rates": {
                            "input_tokens": 1.0,
                            "output_tokens": 2.0,
                        },
                    },
                }
            ]
        },
    )

    assert result["coverage"]["attributed_turn_count"] == 1
    assert result["coverage"]["priced_turn_count"] == 1
    assert result["average_attributed_cost_per_turn"] == {
        "amount": 0.2,
        "currency": "USD",
    }
    assert result["pricing"] == {
        "source": "current-test-pricing",
        "version": "current-v2",
        "effective_at_utc": "2026-08-05T00:00:00Z",
    }


def test_historical_summary_classifies_known_subtotal_as_partial() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    result = build_historical_model_cost_summary(
        [
            {
                "created_at_utc": "2026-08-05T10:00:00Z",
                "llm_usage_cost_summary": {
                    "status": "legacy-projection"
                },
                "llm_calls": [
                    {
                        "call_id": "partial-call",
                        "provider": "openai",
                        "effective_model": "gpt-test",
                        "model_identity_source": "provider_response",
                        "usage": {"prompt_tokens": 100},
                    }
                ],
            }
        ],
        provider="openai",
        model="gpt-test",
        model_registry={
            "models": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "pricing": {
                        "schema_version": "llm_model_pricing.v1",
                        "version": "v2",
                        "source": "test",
                        "effective_at_utc": "2026-08-05T00:00:00Z",
                        "model_id": "gpt-test",
                        "currency": "USD",
                        "unit_tokens": 1_000,
                        "rates": {
                            "input_tokens": 1.0,
                            "output_tokens": 2.0,
                        },
                    },
                }
            ]
        },
    )

    assert result["status"] == "partial"
    assert result["coverage"]["partial_turn_count"] == 1
    assert result["coverage"]["unavailable_turn_count"] == 0
    assert result["average_attributed_cost_per_turn"]["amount"] is None


def test_partial_coverage_does_not_publish_a_misleading_average() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    def record(call_id: str, usage: dict | None) -> dict:
        return {
            "created_at_utc": f"2026-08-0{call_id[-1]}T10:00:00Z",
            "llm_usage_cost_summary": {
                "status": "legacy-projection"
            },
            "llm_calls": [
                {
                    "call_id": call_id,
                    "provider": "openai",
                    "effective_model": "gpt-test",
                    "model_identity_source": "provider_response",
                    "usage": usage,
                }
            ],
        }

    result = build_historical_model_cost_summary(
        [
            record("call-1", {"prompt_tokens": 100, "completion_tokens": 50}),
            record("call-2", None),
        ],
        provider="openai",
        model="gpt-test",
        model_registry={
            "models": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "pricing": {
                        "schema_version": "llm_model_pricing.v1",
                        "version": "v2",
                        "source": "test",
                        "effective_at_utc": "2026-08-05T00:00:00Z",
                        "model_id": "gpt-test",
                        "currency": "USD",
                        "unit_tokens": 1_000,
                        "rates": {
                            "input_tokens": 1.0,
                            "output_tokens": 2.0,
                        },
                    },
                }
            ]
        },
    )

    assert result["status"] == "partial"
    assert result["coverage"]["priced_turn_count"] == 1
    assert result["coverage"]["unavailable_turn_count"] == 1
    assert result["average_attributed_cost_per_turn"] == {
        "amount": None,
        "currency": None,
    }


def test_historical_summary_excludes_unsent_provider_denials() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    result = build_historical_model_cost_summary(
        [
            {
                "created_at_utc": "2026-08-05T10:00:00Z",
                "llm_calls": [
                    {
                        "call_id": "denied",
                        "provider": "openai",
                        "effective_model": "gpt-test",
                        "model_identity_source": "provider_response",
                        "provider_request_sent": False,
                    }
                ],
            },
            {
                "created_at_utc": "2026-08-05T11:00:00Z",
                "llm_calls": [
                    {
                        "call_id": "paid",
                        "provider": "openai",
                        "effective_model": "gpt-test",
                        "model_identity_source": "provider_response",
                        "provider_request_sent": True,
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                    }
                ],
            },
        ],
        provider="openai",
        model="gpt-test",
        model_registry={
            "models": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "pricing": {
                        "schema_version": "llm_model_pricing.v1",
                        "version": "v2",
                        "source": "test",
                        "effective_at_utc": "2026-08-05T00:00:00Z",
                        "model_id": "gpt-test",
                        "currency": "USD",
                        "unit_tokens": 1_000,
                        "rates": {"input_tokens": 1.0, "output_tokens": 2.0},
                    },
                }
            ]
        },
    )

    assert result["status"] == "available"
    assert result["coverage"]["attributed_turn_count"] == 1
    assert result["coverage"]["priced_turn_count"] == 1
    assert result["average_attributed_cost_per_turn"] == {
        "amount": 0.2,
        "currency": "USD",
    }


def test_content_free_summary_remains_repriceable_without_raw_calls() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    result = build_historical_model_cost_summary(
        [
            {
                "created_at_utc": "2026-08-05T10:00:00Z",
                "llm_usage_cost_summary": {
                    "schema_version": "llm_usage_cost_summary.v1",
                    "usage": {
                        "status": "reported",
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 50,
                        "total_tokens": 150,
                    },
                    "model_identities": [
                        {
                            "provider": "openai",
                            "effective_model": "gpt-test",
                            "model_identity_source": "provider_response",
                            "provider_request_sent": True,
                            "effective_service_tier": "default",
                            "connection_id": "#V#openai_provider",
                            "call_count": 2,
                        }
                    ],
                    "model_identity_omitted_count": 0,
                    "billable_call_count": 2,
                    "pricing_usage_groups": [
                        {
                            "provider": "openai",
                            "effective_model": "gpt-test",
                            "model_identity_source": "provider_response",
                            "provider_request_sent": True,
                            "effective_service_tier": "default",
                            "connection_id": "#V#openai_provider",
                            "pricing_context_input_tokens": 50,
                            "usage": {
                                "status": "reported",
                                "input_tokens": 100,
                                "cached_input_tokens": 0,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 50,
                                "total_tokens": 150,
                            },
                            "call_count": 2,
                        }
                    ],
                    "pricing_usage_group_omitted_count": 0,
                    "pricing_usage_group_covered_call_count": 2,
                    "pricing_usage_group_unattributed_call_count": 0,
                },
            }
        ],
        provider="openai",
        model="gpt-test",
        model_registry={
            "models": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "pricing": {
                        "schema_version": "llm_model_pricing.v1",
                        "version": "v2",
                        "source": "test",
                        "effective_at_utc": "2026-08-05T00:00:00Z",
                        "model_id": "gpt-test",
                        "currency": "USD",
                        "unit_tokens": 1_000,
                        "applicability": {
                            "effective_service_tiers": ["default"],
                            "connection_ids": ["#V#openai_provider"],
                        },
                        "rates": {
                            "input_tokens": 1.0,
                            "cached_input_tokens": 0.1,
                            "cache_write_input_tokens": 1.25,
                            "output_tokens": 2.0,
                        },
                    },
                }
            ]
        },
    )

    assert result["status"] == "available"
    assert result["coverage"]["attributed_turn_count"] == 1
    assert result["average_attributed_cost_per_turn"] == {
        "amount": 0.2,
        "currency": "USD",
    }


def test_mixed_content_free_summary_is_attributed_but_not_mispriced() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    result = build_historical_model_cost_summary(
        [
            {
                "created_at_utc": "2026-08-05T10:00:00Z",
                "llm_usage_cost_summary": {
                    "schema_version": "llm_usage_cost_summary.v1",
                    "usage": {
                        "status": "reported",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150,
                    },
                    "model_identities": [
                        {
                            "provider": "openai",
                            "effective_model": "gpt-test",
                            "model_identity_source": "provider_response",
                            "provider_request_sent": True,
                        },
                        {
                            "provider": "openai",
                            "effective_model": "gpt-other",
                            "model_identity_source": "provider_response",
                            "provider_request_sent": True,
                        },
                    ],
                    "model_identity_omitted_count": 0,
                    "billable_call_count": 2,
                    "pricing_usage_groups": [
                        {
                            "provider": "openai",
                            "effective_model": "gpt-test",
                            "model_identity_source": "provider_response",
                            "provider_request_sent": True,
                            "pricing_context_input_tokens": 100,
                            "usage": {
                                "status": "reported",
                                "input_tokens": 100,
                                "output_tokens": 50,
                                "total_tokens": 150,
                            },
                            "call_count": 1,
                        },
                        {
                            "provider": "openai",
                            "effective_model": "gpt-other",
                            "model_identity_source": "provider_response",
                            "provider_request_sent": True,
                            "pricing_context_input_tokens": 100,
                            "usage": {
                                "status": "reported",
                                "input_tokens": 100,
                                "output_tokens": 50,
                                "total_tokens": 150,
                            },
                            "call_count": 1,
                        },
                    ],
                    "pricing_usage_group_omitted_count": 0,
                    "pricing_usage_group_covered_call_count": 2,
                    "pricing_usage_group_unattributed_call_count": 0,
                },
            }
        ],
        provider="openai",
        model="gpt-test",
        model_registry={},
    )

    assert result["status"] == "unavailable"
    assert result["coverage"] == {
        "priced_turn_count": 0,
        "partial_turn_count": 0,
        "unavailable_turn_count": 1,
        "attributed_turn_count": 1,
    }
    assert result["average_attributed_cost_per_turn"]["amount"] is None


def test_actor_history_query_is_bounded_by_actor_namespace_time_and_limit(
    monkeypatch,
) -> None:
    from src.backend.services import llm_model_cost_history_service as service

    captured: dict = {}

    class Cursor(list):
        def sort(self, key, direction):
            captured["sort"] = (key, direction)
            return self

        def limit(self, value):
            captured["limit"] = value
            return self

    class Collection:
        def find(self, query, projection):
            captured["query"] = query
            captured["projection"] = projection
            return Cursor()

    monkeypatch.setattr(
        service, "get_turn_execution_records_collection", lambda: Collection()
    )

    result = service.get_actor_historical_model_cost_summary(
        user_id="#V#person",
        namespace="#V#person@org",
        provider="openai",
        model="gpt-test",
        limit=10_000,
        window_days=10_000,
    )

    assert captured["query"]["user_id"] == "#V#person"
    assert captured["query"]["namespace"] == "#V#person@org"
    assert "$gte" in captured["query"]["created_at_utc"]
    summary_branch = captured["query"]["$or"][0]
    assert summary_branch["llm_usage_cost_summary.schema_version"] == (
        "llm_usage_cost_summary.v1"
    )
    canonical_match = summary_branch[
        "llm_usage_cost_summary.model_identities"
    ]["$elemMatch"]
    assert canonical_match["provider"] == "openai"
    assert canonical_match["effective_model"] == "gpt-test"
    assert canonical_match["model_identity_source"] == "provider_response"
    assert canonical_match["provider_request_sent"] == {"$ne": False}
    legacy_match = captured["query"]["$or"][1]["llm_calls"]["$elemMatch"]
    assert legacy_match == canonical_match
    assert captured["projection"]["llm_usage_cost_summary"] == 1
    assert captured["sort"] == ("created_at_utc", -1)
    assert captured["limit"] == 500
    assert result["sample_window"]["limit"] == 500
    assert result["sample_window"]["window_days"] == 365
