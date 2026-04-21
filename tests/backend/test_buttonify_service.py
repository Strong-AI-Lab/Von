from src.backend.services.buttonify_service import (
    enforce_buttonify_prompt_contract,
    parse_buttonify_options_json_with_telemetry,
    sanitise_buttonify_options,
)


def test_buttonify_structured_json_filters_invalid_candidates() -> None:
    options, telemetry = parse_buttonify_options_json_with_telemetry(
        '["Proceed", "#V#academic_conference", "Hold", "JVNAUTOSCI-1219"]'
    )

    assert options == ["Proceed", "Hold"]
    assert telemetry["parse_reason"] == "ok"
    assert telemetry["accepted_candidate_count"] == 2
    assert telemetry["rejection_reason_counts"]["code_or_identifier"] == 2


def test_buttonify_structured_json_fails_closed_on_prose() -> None:
    options, telemetry = parse_buttonify_options_json_with_telemetry(
        'Please reply with one of: "Proceed", "Hold".'
    )

    assert options == []
    assert telemetry["parse_success"] is False
    assert telemetry["parse_reason"].startswith("json_decode_failed:")


def test_buttonify_sanitise_filters_code_like_values() -> None:
    options = sanitise_buttonify_options(
        [
            "#V#academic_conference",
            "Proceed",
            "research_symposium",
            "Show options",
            "JVNAUTOSCI-1219",
        ]
    )
    assert options == ["Proceed", "Show options"]


def test_buttonify_prompt_contract_suffix_is_applied_once() -> None:
    prompt = "Return only JSON."
    once = enforce_buttonify_prompt_contract(prompt)
    twice = enforce_buttonify_prompt_contract(once)
    assert "Mandatory quality gate" in once
    assert once == twice
