"""Defines block templates for Slack messages"""

from typing import List


def recommendation_block(custom_message: str) -> List:
    """A block template for a recommendation message.

    Args:
        custom_message: A custom message to be displayed before the recommendation.

    Returns:
        A list of objects that define the blocks of the message.
    """
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": custom_message},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Do you agree with the recommendation decision? Please provide feedback below.",
            },
        },
        {
            "type": "actions",
            "block_id": "user_feedback",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Yes!"},
                    "style": "primary",
                    "action_id": "feedback_positive",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Nope..."},
                    "action_id": "feedback_negative",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Additional Feedback"},
                    "action_id": "feedback_explanation",
                },
            ],
        },
    ]
    return blocks


def positive_recommendation_block(user_id: str, url: str, explanation: str) -> List:
    """A block template for a positive recommendation message.

    Args:
        user_id: The user ID.
        url: The URL of the recommended paper.
        explanation: The explanation of the recommendation decision.

    Returns:
        A list of objects that define the blocks of the message.
    """
    blocks = recommendation_block(
        f":spock-hand: Hi there, <@{user_id}>! Here is a paper you might be interested in: {url}\n{explanation}",
    )
    return blocks


def negative_recommendation_block(user_id: str, url: str, explanation: str) -> List:
    """A block template for a negative recommendation message.

    Args:
        user_id: The user ID. Not used at the moment.
        url: The URL of the recommended paper.
        explanation: The explanation of the recommendation decision.

    Returns:
        A list of objects that define the blocks of the message.
    """
    blocks = recommendation_block(
        f"I don't think this paper is relevant to you, but you might disagree: {url}\n{explanation}",
    )
    return blocks
