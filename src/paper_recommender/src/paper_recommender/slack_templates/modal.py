"""Defines Slack modal templates """


def project_modal() -> dict:
    """Returns a dictionary as the modal surface for entering a project description."""
    view = {
        "type": "modal",
        # View identifier
        "callback_id": "project_modal",
        "title": {"type": "plain_text", "text": "paper_recommender"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "blocks": [
            {
                "type": "input",
                "block_id": "input_title",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "sl_input",
                    "max_length": 250,
                    "placeholder": {"type": "plain_text", "text": "Enter your project title here..."},
                },
                "label": {"type": "plain_text", "text": "Project Title"},
            },
            {
                "type": "input",
                "block_id": "input_description",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "ml_input",
                    "multiline": True,
                    "max_length": 3000,
                    "placeholder": {"type": "plain_text", "text": "Enter your project description here..."},
                },
                "label": {"type": "plain_text", "text": "Project Description"},
            },
        ],
    }
    return view


def feedback_modal(recommendation_id: str) -> dict:
    """Returns a dictionary that defines the modal surface for entering user feedback
    on the specified recommendation."""
    view = {
        "type": "modal",
        # View identifier
        "callback_id": "feedback_modal",
        "title": {"type": "plain_text", "text": "paper_recommender"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "blocks": [
            {
                "type": "input",
                "block_id": "user_input_feedback",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "user_feedback_input",
                    "multiline": True,
                    "max_length": 3000,
                    "placeholder": {"type": "plain_text", "text": "Enter your input here..."},
                },
                "label": {
                    "type": "plain_text",
                    "text": "Please provide additional feedback here.",
                },
            }
        ],
        "private_metadata": recommendation_id,
    }
    return view
