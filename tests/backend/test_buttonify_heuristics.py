from src.backend.services.buttonify_service import (
    extract_buttonify_options_heuristic,
    enforce_buttonify_prompt_contract,
    sanitise_buttonify_heuristic_options,
    select_buttonify_preflight_options,
)


def test_buttonify_heuristic_extracts_quotes():
    text = 'Please reply with one of: "Proceed", "Hold", "Stop".'
    options = extract_buttonify_options_heuristic(text)
    assert options == ["Proceed", "Hold", "Stop"]


def test_buttonify_heuristic_extracts_list_items():
    text = """
Tell me how you want to proceed:
- Draft summary
- Send update
- Pause work
"""
    options = extract_buttonify_options_heuristic(text)
    assert options == ["Draft summary", "Send update", "Pause work"]


def test_buttonify_heuristic_extracts_implicit_or_question():
    text = "Would you like to continue or stop?"
    options = extract_buttonify_options_heuristic(text)
    assert options == ["continue", "stop"]


def test_buttonify_heuristic_yes_no_questions():
    text = "Is that ok?"
    options = extract_buttonify_options_heuristic(text)
    assert options == ["Yes", "No"]


def test_buttonify_preflight_rejects_code_like_candidates():
    text = """
Please pick one:
- `#V#academic_conference`
- `#V#research_symposium`
"""
    candidates = extract_buttonify_options_heuristic(text)
    options, reason = select_buttonify_preflight_options(candidates)
    assert options == []
    assert reason == "heuristic_preflight_rejected_code_like_candidates"


def test_buttonify_sanitise_filters_code_like_values():
    options = sanitise_buttonify_heuristic_options(
        [
            "#V#academic_conference",
            "Proceed",
            "research_symposium",
            "Show options",
            "JVNAUTOSCI-1219",
        ]
    )
    assert options == ["Proceed", "Show options"]


def test_buttonify_prompt_contract_suffix_is_applied_once():
    prompt = "Return only JSON."
    once = enforce_buttonify_prompt_contract(prompt)
    twice = enforce_buttonify_prompt_contract(once)
    assert "Mandatory quality gate" in once
    assert once == twice
