"""Defines view templates for the Slack home tab."""

from typing import List, Union


def project_view(project_id: str, project_description: str, title: Union[str, None]) -> List:
    """Prepares a block element for a project.

    Args:
        project_id: The project ID.
        project_description: Project description in Python string.
        title: The title of the project.

    Returns:
        A dictionary representing a Slack home view.
    """
    view = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": title if title else "Your current project",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": project_description,
                "emoji": True,
            },
        },
        {
            "type": "actions",
            "block_id": project_id,
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Delete",
                        "emoji": True,
                    },
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Are you sure?"},
                        "text": {"type": "plain_text", "text": "Proceed by clicking the 'Confirm' button."},
                        "confirm": {"type": "plain_text", "text": "Confirm"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                    "style": "danger",
                    "action_id": "delete_project",
                },
            ],
        },
        {"type": "divider"},
    ]
    return view


def vip_club_view() -> List:
    """Prepares a block element for the Lab-Rats VIP Club."""
    view = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Lab-Rats VIP",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "By default paper_recommender only sends a shared paper to you if it thinks it is relevant to "
                    "your project. By joining the secretive, exclusive and prestigious *Lab-Rats VIP Club*, "
                    "you will be sent papers that paper_recommender deemed irrelevant to you, along with its explanations. "
                    "Additionally, you will gain early access to new and more advanced features as they are "
                    "added to paper_recommender."
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "vip_club",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "emoji": True,
                        "text": "Join :mouse2:",
                    },
                    "style": "primary",
                    "action_id": "join_club",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "emoji": True,
                        "text": "Leave :broken_heart:",
                    },
                    "style": "danger",
                    "action_id": "leave_club",
                },
            ],
        },
    ]
    return view


def add_project_block(custom_message: str) -> List:
    """Prepares a block element for adding project."""
    view = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Add Project",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": custom_message,
                "emoji": True,
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Add",
                        "emoji": True,
                    },
                    "action_id": "add_project",
                }
            ],
        },
        {"type": "divider"},
    ]
    return view


def home_view(projects: List[dict], user_exists: bool) -> dict:
    """Prepares a Slack home view.

    If the user exists, display the project description and information about the Lab-Rats VIP Club.
    Otherwise, only display the project description, which could be an empty string .

    Args:
        projects: A list of projects in dictionaries. The project is assumed to have 'project_id',
            'description', and 'title' keys.
        user_exists: A boolean value indicating if the user exists in the database.

    Returns:
        A dictionary representing a Slack home view.
    """
    if user_exists:
        projects_blocks = []
        for project in projects:
            projects_blocks += project_view(str(project["project_id"]), project["description"], project["title"])
        view = {
            "type": "home",
            "blocks": projects_blocks + add_project_block("Feel free to add more projects!") + vip_club_view(),
        }
    else:
        view = {"type": "home", "blocks": add_project_block("Register by adding a project!")}
    return view
