"""Implementation of the Nature class to extract paper information from nature articles, using webscraping.

For example: https://www.nature.com/articles/s41586-023-06004-9
NOTE: The current implementation is specifically for the "Nature" journal only.
Other journals from nature.com might have different html structure or paper format, therefore may not work properly.
"""

import logging
from typing import Union, List
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from .base import PaperExtractionBase, Paper

logger = logging.getLogger(__name__)


class Nature(PaperExtractionBase):
    """Extract paper information from Nature."""

    DOMAIN = "www.nature.com"

    @staticmethod
    def extract_from_url(url: str) -> Union[Paper, None]:
        """Extract paper information from Nature. Must return base.Paper dataclass.

        Args:
            url (str): URL of the paper to be extracted.

        Returns:
            Paper dataclass from base.py. Or None if any error or exception occured.

        NOTE: We assume that the provided URL is an Nature URL. It is the responsibility of the caller to
        make sure this is the case. However this class does provide a method to check this assumption.
        """
        response = requests.get(url, timeout=10)

        if response.status_code != 200:
            logger.error("Failed to retrieve paper using the provided URL: %s", url)
            return None

        soup = BeautifulSoup(response.content, "html.parser")
        abstract = Nature._extract_abstract(soup)
        title = Nature._extract_title(soup)
        authors = Nature._extract_authors(soup)
        if not abstract or not title or not authors:
            # errors should have been caught and logged in the helper methods
            logger.error("Failed to extract paper information from the provided URL: %s", url)
            return None

        paper = Paper(
            url=url,
            title=title,
            authors=authors,
            abstract=abstract,
        )
        return paper

    @staticmethod
    def _extract_abstract(soup: BeautifulSoup) -> Union[str, None]:
        """Extract abstract from BeautifulSoup object created using the Nature article URL.

        Args:
            soup: BeautifulSoup object created using the Nature article URL.

        Returns:
            A string containing the abstract. If fails to extract abstract, return None."""
        try:
            abstract = soup.find(id="Abs1-content")
            assert abstract, "Failed to find abstract in the provided URL. The HTML structure has changed."
            for item in abstract.find_all("sup"):  # type: ignore
                item.extract()
            return abstract.text.strip()
        except Exception:
            logger.exception("Failed to extract abstract from the provided URL.")
            return None

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> Union[str, None]:
        """Extract title from BeautifulSoup object created using the Nature article URL.

        Args:
            soup: BeautifulSoup object created using the Nature article URL.

        Returns:
            A string containing the title. If fails to extract title, return None."""
        try:
            return soup.find(attrs={"data-article-title": True}).text.strip()  # type: ignore
        except Exception:
            logger.exception("Failed to extract title from the provided URL. The HTML structure may have changed.")
            return None

    @staticmethod
    def _extract_authors(soup: BeautifulSoup) -> Union[List[str], None]:
        """Extract authors from BeautifulSoup object created using the Nature article URL.

        Args:
            soup: BeautifulSoup object created using the Nature article URL.

        Returns:
            A list of strings containing the authors. If fails to extract authors, return None."""
        try:
            authors = soup.find(attrs={"data-component-authors-activator": "authors-list"}).find_all(  # type: ignore
                attrs={"data-test": "author-name"}
            )
            assert len(authors) > 0, "Failed to find authors in the provided URL. The HTML structure has changed."
            return [author.text.strip() for author in authors]
        except Exception:
            logger.exception("Failed to extract authors from the provided URL. The HTML structure may have changed.")
            return None

    @staticmethod
    def _domain_check(url: str) -> bool:
        """Check if the provided url is from Nature.

        Args:
            url: a URL string.

        Returns:
            True if the provided url is from Nature. False otherwise.
        """
        parsed_url = urlparse(url)
        return parsed_url.netloc == Nature.DOMAIN
