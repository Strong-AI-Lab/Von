"""Defines a common interface for paper extraction using abstract class."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Union


@dataclass
class Paper:
    """Data class for the extracted paper."""

    url: str
    title: str
    authors: List[str]
    abstract: str


class PaperExtractionBase(ABC):
    """Base class for paper extraction.

    All paper extraction classes should inherit from
    this class and implement the extract_from_url method."""

    # The domain name of the source
    # It must be defined in the child classes, however, type checking tools like mypy will not throw an error.
    # There is no standard way to define constant class attribute for abstract class in Python.
    DOMAIN: str

    @staticmethod
    @abstractmethod
    def extract_from_url(url: str) -> Union[Paper, None]:
        """Extract paper information from the provided URL.

        This method must be implemented by the child classes.
        It should be a static method that does not reply on instance attributes.

        Args:
            url (str): URL of the paper to be extracted.

        Returns:
            Paper dataclass. Or None if any error or exception occured."""
        raise NotImplementedError
