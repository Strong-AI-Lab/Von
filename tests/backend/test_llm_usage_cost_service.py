from __future__ import annotations

from datetime import UTC, datetime

from src.backend.services.llm_usage_cost_service import (
    build_llm_usage_cost_summary,
    normalise_llm_usage,
)


def _registry(*, model_id: str = "gpt-5-test") -> dict:
    return {
        "source": "test_registry",
        "models": [
            {
                "provider": "openai",
                "model_id": model_id,
                "pricing": {
                    "schema_version": "llm_model_pricing.v1",
                    "version": "test-2026-08-05",
                    "source": "test_fixture",
                    "effective_at_utc": "2026-08-05T00:00:00Z",
                    "model_id": model_id,
                    "currency": "USD",
                    "unit_tokens": 1_000_000,
                    "rates": {
                        "input_tokens": 2.0,
                        "output_tokens": 8.0,
                    },
                },
            }
        ],
    }


def _expiring_gemini_registry() -> dict:
    return {
        "models": [
            {
                "provider": "gemini",
                "model_id": "gemini-3.7-flash",
                "pricing": {
                    "schema_version": "llm_model_pricing.v1",
                    "version": "gemini-standard-introductory-2026-08-13",
                    "source": "https://ai.google.dev/gemini-api/docs/pricing",
                    "effective_at_utc": "2026-08-13T00:00:00Z",
                    "effective_until_utc": "2027-01-01T00:00:00Z",
                    "model_id": "gemini-3.7-flash",
                    "currency": "USD",
                    "unit_tokens": 1_000_000,
                    "applicability": {
                        "effective_service_tiers": ["standard"],
                        "connection_ids": ["gemini_developer_api"],
                    },
                    "rates": {
                        "input_tokens": 0.75,
                        "output_tokens": 3.75,
                    },
                },
            }
        ]
    }


def _priced_gemini_call(
    *, tier: str = "standard", connection_id: str = "gemini_developer_api"
) -> dict:
    return {
        "call_id": "gemini-call",
        "provider": "gemini",
        "effective_model": "gemini-3.7-flash",
        "model_identity_source": "provider_response",
        "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "transport": {
            "effective_service_tier": tier,
            "effective_connection_id": connection_id,
        },
    }


def test_normalise_usage_accepts_existing_token_field_names() -> None:
    usage = normalise_llm_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 40,
        }
    )

    assert usage == {
        "status": "reported",
        "input_tokens": 100,
        "cached_input_tokens": None,
        "cache_write_input_tokens": None,
        "output_tokens": 40,
        "total_tokens": 140,
    }


def _detailed_registry(*, model_id: str = "gpt-5.6-terra") -> dict:
    return {
        "models": [
            {
                "provider": "openai",
                "model_id": model_id,
                "pricing": {
                    "schema_version": "llm_model_pricing.v1",
                    "version": "openai-standard-2026-08-05",
                    "source": "https://developers.openai.com/api/docs/pricing",
                    "effective_at_utc": "2026-08-05T00:00:00Z",
                    "model_id": model_id,
                    "currency": "USD",
                    "unit_tokens": 1_000_000,
                    "applicability": {
                        "effective_service_tiers": ["default"],
                        "connection_ids": ["#V#openai_provider"],
                    },
                    "rates": {
                        "input_tokens": 2.0,
                        "cached_input_tokens": 0.2,
                        "cache_write_input_tokens": 2.5,
                        "output_tokens": 12.0,
                    },
                    "long_context": {
                        "threshold_input_tokens": 272_000,
                        "rates": {
                            "input_tokens": 4.0,
                            "cached_input_tokens": 0.4,
                            "cache_write_input_tokens": 5.0,
                            "output_tokens": 18.0,
                        },
                    },
                },
            }
        ]
    }


def _priced_terra_call(*, usage: dict, tier: str = "default") -> dict:
    return {
        "call_id": "terra-call",
        "provider": "openai",
        "effective_model": "gpt-5.6-terra",
        "model_identity_source": "provider_response",
        "usage": usage,
        "transport": {
            "effective_service_tier": tier,
            "effective_connection_id": "#V#openai_provider",
        },
    }


def test_detailed_openai_price_uses_cache_breakdown_and_context_band() -> None:
    short = build_llm_usage_cost_summary(
        [
            _priced_terra_call(
                usage={
                    "prompt_tokens": 1_000,
                    "cached_input_tokens": 200,
                    "cache_write_input_tokens": 100,
                    "completion_tokens": 100,
                }
            )
        ],
        model_registry=_detailed_registry(),
    )
    assert short["estimated_cost"]["status"] == "estimated"
    assert short["estimated_cost"]["amount"] == 0.00289

    long = build_llm_usage_cost_summary(
        [
            _priced_terra_call(
                usage={
                    "prompt_tokens": 300_000,
                    "cached_input_tokens": 100_000,
                    "cache_write_input_tokens": 50_000,
                    "completion_tokens": 1_000,
                }
            )
        ],
        model_registry=_detailed_registry(),
    )
    assert long["estimated_cost"]["status"] == "estimated"
    assert long["estimated_cost"]["amount"] == 0.908


def test_detailed_price_is_partial_without_cache_breakdown() -> None:
    summary = build_llm_usage_cost_summary(
        [
            _priced_terra_call(
                usage={"prompt_tokens": 1_000, "completion_tokens": 100}
            )
        ],
        model_registry=_detailed_registry(),
    )

    assert summary["estimated_cost"]["status"] == "partial"
    assert summary["estimated_cost"]["amount"] is None
    assert summary["estimated_cost"]["known_amount"] == 0.0012


def test_detailed_price_requires_effective_tier_and_direct_connection() -> None:
    missing_tier = _priced_terra_call(
        usage={
            "prompt_tokens": 1_000,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "completion_tokens": 100,
        },
        tier="",
    )
    unknown_connection = _priced_terra_call(usage=dict(missing_tier["usage"]))
    unknown_connection["transport"]["effective_connection_id"] = "compatible:opaque"

    for call in (missing_tier, unknown_connection):
        summary = build_llm_usage_cost_summary(
            [call],
            model_registry=_detailed_registry(),
        )
        assert summary["estimated_cost"]["status"] == "unavailable"
        assert summary["estimated_cost"]["amount"] is None


def test_expiring_gemini_price_requires_exact_standard_tier_and_connection() -> None:
    as_of = datetime(2026, 8, 21, tzinfo=UTC)
    standard = build_llm_usage_cost_summary(
        [_priced_gemini_call()],
        model_registry=_expiring_gemini_registry(),
        as_of=as_of,
    )
    priority = build_llm_usage_cost_summary(
        [_priced_gemini_call(tier="priority")],
        model_registry=_expiring_gemini_registry(),
        as_of=as_of,
    )
    wrong_connection = build_llm_usage_cost_summary(
        [_priced_gemini_call(connection_id="gemini_vertex_ai")],
        model_registry=_expiring_gemini_registry(),
        as_of=as_of,
    )

    assert standard["estimated_cost"]["status"] == "estimated"
    assert standard["estimated_cost"]["amount"] == 4.5
    assert priority["estimated_cost"]["status"] == "unavailable"
    assert priority["estimated_cost"]["unavailable_reasons"] == [
        "effective_service_tier_unpriced"
    ]
    assert wrong_connection["estimated_cost"]["status"] == "unavailable"
    assert wrong_connection["estimated_cost"]["unavailable_reasons"] == [
        "provider_connection_unpriced"
    ]


def test_expiring_price_fails_closed_at_effective_until_boundary() -> None:
    summary = build_llm_usage_cost_summary(
        [_priced_gemini_call()],
        model_registry=_expiring_gemini_registry(),
        as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert summary["estimated_cost"]["status"] == "unavailable"
    assert summary["estimated_cost"]["amount"] is None
    assert summary["estimated_cost"]["unavailable_reasons"] == ["pricing_expired"]


def test_cost_uses_effective_model_and_exact_registry_match() -> None:
    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "call-1",
                "provider": "openai",
                "requested_model": "gpt-5-requested",
                "selected_model": "gpt-5-selected",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
        ],
        model_registry=_registry(),
    )

    assert summary["estimated_cost"]["status"] == "estimated"
    assert summary["estimated_cost"]["amount"] == 0.00052
    assert summary["model_identities"] == [
        {
            "provider": "openai",
            "requested_model": "gpt-5-requested",
            "selected_model": "gpt-5-selected",
            "effective_model": "gpt-5-test",
            "model_identity_source": "provider_response",
            "provider_request_sent": None,
            "effective_service_tier": None,
            "connection_id": None,
            "call_count": 1,
        }
    ]

    family_only = build_llm_usage_cost_summary(
        [
            {
                "call_id": "call-2",
                "provider": "openai",
                "effective_model": "gpt-5-test-2026-08-05",
                "model_identity_source": "provider_response",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
        ],
        model_registry=_registry(),
    )
    assert family_only["estimated_cost"]["status"] == "unavailable"
    assert family_only["estimated_cost"]["amount"] is None
    assert family_only["model_identities"][0]["effective_model"] == (
        "gpt-5-test-2026-08-05"
    )


def test_stable_call_id_is_counted_once_and_richer_record_wins() -> None:
    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "same-attempt",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "status": "timeout_observed",
                "usage": None,
            },
            {
                "call_id": "same-attempt",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "status": "completed",
                "usage": {"prompt_tokens": 1_000, "completion_tokens": 500},
            },
        ],
        model_registry=_registry(),
    )

    assert summary["call_count"] == 2
    assert summary["unique_call_count"] == 1
    assert summary["duplicate_call_count"] == 1
    assert summary["usage"]["total_tokens"] == 1_500
    assert summary["estimated_cost"]["amount"] == 0.006
    assert summary["model_identities"][0]["call_count"] == 1


def test_calls_without_stable_ids_remain_distinct() -> None:
    call = {
        "provider": "openai",
        "effective_model": "gpt-5-test",
        "model_identity_source": "provider_response",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    summary = build_llm_usage_cost_summary(
        [dict(call), dict(call)], model_registry=_registry()
    )

    assert summary["unique_call_count"] == 2
    assert summary["duplicate_call_count"] == 0
    assert summary["usage"]["total_tokens"] == 300


def test_missing_usage_or_pricing_is_unavailable_not_zero() -> None:
    missing_usage = build_llm_usage_cost_summary(
        [
            {
                "call_id": "call-1",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "usage": None,
            }
        ],
        model_registry=_registry(),
    )
    assert missing_usage["usage"]["status"] == "unavailable"
    assert missing_usage["usage"]["total_tokens"] is None
    assert missing_usage["estimated_cost"]["status"] == "unavailable"
    assert missing_usage["estimated_cost"]["amount"] is None
    assert missing_usage["estimated_cost"]["known_amount"] is None

    missing_pricing = build_llm_usage_cost_summary(
        [
            {
                "call_id": "call-2",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
        ]
    )
    assert missing_pricing["usage"]["status"] == "reported"
    assert missing_pricing["estimated_cost"]["status"] == "unavailable"
    assert missing_pricing["estimated_cost"]["amount"] is None


def test_missing_effective_model_is_not_priced_from_legacy_model_field() -> None:
    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "legacy-identity",
                "provider": "openai",
                "model": "gpt-5-test",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
        ],
        model_registry=_registry(),
    )

    assert summary["model_identities"][0]["effective_model"] is None
    assert summary["estimated_cost"]["status"] == "unavailable"
    assert summary["estimated_cost"]["amount"] is None
    assert "effective_model_unavailable" in summary["estimated_cost"][
        "unavailable_reasons"
    ]


def test_selected_request_identity_is_not_priced_as_provider_effective() -> None:
    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "selected-only",
                "provider": "openai",
                "selected_model": "gpt-5-test",
                "effective_model": "gpt-5-test",
                "model_identity_source": "selected_request",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
        ],
        model_registry=_registry(),
    )

    assert summary["estimated_cost"]["status"] == "unavailable"
    assert summary["estimated_cost"]["amount"] is None
    assert summary["pricing_usage_groups"] == []
    assert summary["pricing_usage_group_unattributed_call_count"] == 1


def test_historical_repricing_preserves_per_call_context_bands() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    first = _priced_terra_call(
        usage={
            "prompt_tokens": 150_000,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "completion_tokens": 0,
        }
    )
    second = {**first, "call_id": "terra-call-2"}
    summary = build_llm_usage_cost_summary(
        [first, second], model_registry=_detailed_registry()
    )
    assert summary["estimated_cost"]["amount"] == 0.6
    assert summary["pricing_usage_groups"][0][
        "pricing_context_input_tokens"
    ] == 150_000
    assert summary["pricing_usage_groups"][0]["usage"]["input_tokens"] == 300_000

    historical = build_historical_model_cost_summary(
        [
            {
                "created_at_utc": "2026-08-05T10:00:00Z",
                "llm_usage_cost_summary": summary,
            }
        ],
        provider="openai",
        model="gpt-5.6-terra",
        model_registry=_detailed_registry(),
    )

    assert historical["status"] == "available"
    assert historical["average_attributed_cost_per_turn"] == {
        "amount": 0.6,
        "currency": "USD",
    }


def test_unattributed_paid_call_prevents_full_turn_cost_estimate() -> None:
    from src.backend.services.llm_model_cost_history_service import (
        build_historical_model_cost_summary,
    )

    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "main",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "provider_request_sent": True,
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
            {
                "call_id": "support",
                "provider": "openai",
                "selected_model": "gpt-5-test",
                "provider_request_sent": True,
                "usage": None,
            },
        ],
        model_registry=_registry(),
    )
    assert summary["billable_call_count"] == 2
    assert summary["pricing_usage_group_covered_call_count"] == 1
    assert summary["pricing_usage_group_unattributed_call_count"] == 1

    historical = build_historical_model_cost_summary(
        [
            {
                "created_at_utc": "2026-08-05T10:00:00Z",
                "llm_usage_cost_summary": summary,
            }
        ],
        provider="openai",
        model="gpt-5-test",
        model_registry=_registry(),
    )
    assert historical["status"] == "unavailable"
    assert historical["average_attributed_cost_per_turn"]["amount"] is None


def test_partial_cost_exposes_only_a_labelled_known_subtotal() -> None:
    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "priced",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
            {
                "call_id": "unknown",
                "provider": "openai",
                "effective_model": "gpt-5-unpriced",
                "model_identity_source": "provider_response",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        ],
        model_registry=_registry(),
    )

    assert summary["estimated_cost"]["status"] == "partial"
    assert summary["estimated_cost"]["amount"] is None
    assert summary["estimated_cost"]["known_amount"] == 0.00052
    assert summary["estimated_cost"]["priced_call_count"] == 1
    assert summary["estimated_cost"]["unpriced_call_count"] == 1


def test_unsent_provider_request_is_not_billable() -> None:
    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "reachability-skip",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "provider_request_sent": False,
                "usage": None,
            }
        ],
        model_registry=_registry(),
    )

    assert summary["billable_call_count"] == 0
    assert summary["usage"]["status"] == "not_applicable"
    assert summary["estimated_cost"]["status"] == "not_applicable"
    assert summary["estimated_cost"]["amount"] is None


def test_local_ollama_usage_is_reported_but_financial_cost_is_not_applicable() -> None:
    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "local-call",
                "provider": "ollama",
                "effective_model": "gemma4:31b",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ]
    )

    assert summary["usage"]["status"] == "reported"
    assert summary["usage"]["total_tokens"] == 120
    assert summary["billable_call_count"] == 0
    assert summary["estimated_cost"]["status"] == "not_applicable"
    assert summary["estimated_cost"]["amount"] is None


def test_unrepresentably_large_price_is_unavailable_instead_of_raising() -> None:
    registry = _registry()
    registry["models"][0]["pricing"]["rates"]["input_tokens"] = "1e999999"

    summary = build_llm_usage_cost_summary(
        [
            {
                "call_id": "huge-price",
                "provider": "openai",
                "effective_model": "gpt-5-test",
                "model_identity_source": "provider_response",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
        ],
        model_registry=registry,
    )

    assert summary["estimated_cost"]["status"] == "unavailable"
    assert summary["estimated_cost"]["amount"] is None
    assert summary["estimated_cost"]["known_amount"] is None
    assert "calls" not in summary
