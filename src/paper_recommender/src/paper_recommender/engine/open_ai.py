"""Implement paper recommendation and explanation using OpenAI Chat Completions API."""

import logging
import string
from typing import List, Union
from openai import OpenAI
from .base import RecommendationOutput

logger = logging.getLogger(__name__)

client = OpenAI()


def prompt_if_worth_reading(project_description: str, paper_abstract: str) -> List:
    """Create a prompt to ask the assistant whether a research paper is worth reading
    based on the project description and the paper's abstract.

    Args:
        project_description (str): Project description.
        paper_abstract (str): Paper abstract.

    Returns:
        A list of dictionaries, each containing the role and the content of the message."""
    prompt = [
        {
            "role": "system",
            "content": (
                "You help descide whether a research paper is worth reading based on my project description "
                "and the paper's abstract. You only recommand a paper if you think it is closely relevant "
                "to the project."
            ),
        },
        {"role": "user", "content": f"Here is a project description of my current project: {project_description}"},
        {"role": "user", "content": f"And here is an abstract of a research paper: {paper_abstract}"},
        {
            "role": "user",
            "content": (
                "Do you think the research paper is relevant and useful for my project and therefore is worth reading? "
                "Please answer with 'Yes' or 'No' only, without any punctuation."
            ),
        },
    ]
    return prompt


def prompt_explain_decision(project_description: str, paper_abstract: str, decision: str) -> List:
    """Create a prompt to ask the model to explain the recommendation decision.

    Args:
        project_description: Project description.
        paper_abstract: Paper abstract.
        decision: Decision made by the assistant in string.

    Returns:
        A list of dictionaries, each containing the role and the content of the message."""
    prompt = prompt_if_worth_reading(project_description, paper_abstract)
    follow_up = [
        {"role": "assistant", "content": decision},
        {"role": "user", "content": "Please explain your decision."},
    ]
    return prompt + follow_up


def paper_recommendation(
    project_description: str,
    paper_abstract: str,
    configs: dict,
) -> Union[RecommendationOutput, None]:
    """Use OpenAI Chat Completions API to recommend a paper based on the project description and the paper abstract.

    Args:
        project_description (str): Project description.
        paper_abstract (str): Paper abstract.
    Return:
        A tuple containing the decision and the explanation. If fails to extract arxiv id, return None.
    """
    try:
        response = client.chat.completions.create(
            model=configs["Engine"]["model"], messages=prompt_if_worth_reading(project_description, paper_abstract)
        )
        decision = response.choices[0].message.content  # type: Union[str, None, bool]
        assert isinstance(decision, str)

        response = client.chat.completions.create(
            model=configs["Engine"]["model"],
            messages=prompt_explain_decision(project_description, paper_abstract, decision),
        )
        explanation = response.choices[0].message.content
        assert isinstance(explanation, str)

        decision = True if decision.lower().translate(str.maketrans("", "", string.punctuation)) == "yes" else False
        return RecommendationOutput(decision=decision, explanation=explanation)
    except Exception:
        logger.exception("Something went wrong while using OpenAI Chat Completions API.")
        return None
