from src.backend.server.routes.von_routes import _extract_buttonify_options_heuristic


def test_buttonify_heuristic_extracts_quotes():
    text = 'Please reply with one of: "Proceed", "Hold", "Stop".'
    options = _extract_buttonify_options_heuristic(text)
    assert options == ["Proceed", "Hold", "Stop"]


def test_buttonify_heuristic_extracts_list_items():
    text = """
Tell me how you want to proceed:
- Draft summary
- Send update
- Pause work
"""
    options = _extract_buttonify_options_heuristic(text)
    assert options == ["Draft summary", "Send update", "Pause work"]


def test_buttonify_heuristic_extracts_implicit_or_question():
    text = "Would you like to continue or stop?"
    options = _extract_buttonify_options_heuristic(text)
    assert options == ["continue", "stop"]


def test_buttonify_heuristic_yes_no_questions():
    text = "Is that ok?"
    options = _extract_buttonify_options_heuristic(text)
    assert options == ["Yes", "No"]
