"""Defines a common interface for paper recommendation."""

from dataclasses import dataclass


@dataclass
class RecommendationOutput:
    """Data class for the recommendation output.

    Attributes:
        decision: A boolean indicating if the recommendation decision is positive or not.
        explanation: A Python string for explaining the decision."""

    decision: bool
    explanation: str
