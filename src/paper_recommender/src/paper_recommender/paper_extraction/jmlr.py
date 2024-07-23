"""Implementation of the JMLR class to extract paper information from JMLR
(Journal of Machine Learning Research) articles using webscraping.

For example: https://www.jmlr.org/papers/v25/23-1042.html
"""

import logging
from typing import Union, List
import requests
from bs4 import BeautifulSoup
from .base import PaperExtractionBase, Paper

logger = logging.getLogger(__name__)


class JMLR(PaperExtractionBase):
    """Extract paper information from JMLR articles."""

    DOMAIN = "www.jmlr.org"

    @staticmethod
    def extract_from_url(url: str) -> Union[Paper, None]:
        """Extract paper information from JMLR. Must return base.Paper dataclass.

        Args:
            url (str): URL of the paper to be extracted.

        Returns:
            Paper dataclass from base.py. Or None if any error or exception occured.

        NOTE: We assume that the provided URL is for an JMLR article. It is the responsibility of the caller to
        make sure this is the case.
        """
        response = requests.get(url, timeout=10)

        if response.status_code != 200:
            logger.error("Failed to retrieve paper using the provided URL: %s", url)
            return None

        soup = BeautifulSoup(response.content, "html.parser")
        abstract = JMLR._extract_abstract(soup)
        title = JMLR._extract_title(soup)
        authors = JMLR._extract_authors(soup)
        if not abstract or not title or not authors:
            # errors should have been caught and logged in the helper methods
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
        """Extract abstract from BeautifulSoup object created using the JMLR article URL.

        Args:
            soup: BeautifulSoup object created using the JMLR article URL.

        Returns:
            A string containing the abstract. If fails to extract abstract, return None."""
        try:
            return soup.find(id="content").find(attrs={"class": "abstract"}).text.strip()  # type: ignore
        except Exception:
            logger.exception("Failed to extract abstract from the provided URL. The HTML structure may have changed.")
            return None

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> Union[str, None]:
        """Extract title from BeautifulSoup object created using the JMLR article URL.

        Args:
            soup: BeautifulSoup object created using the JMLR article URL.

        Returns:
            A string containing the title. If fails to extract title, return None."""
        try:
            return soup.find(id="content").h2.text.strip()  # type: ignore
        except Exception:
            logger.exception("Failed to extract title from the provided URL. The HTML structure may have changed.")
            return None

    @staticmethod
    def _extract_authors(soup: BeautifulSoup) -> Union[List[str], None]:
        """Extract authors from BeautifulSoup object created using the JMLR article URL.

        Args:
            soup: BeautifulSoup object created using the JMLR article URL.

        Returns:
            A list of strings containing the authors. If fails to extract authors, return None."""
        try:
            authors = soup.find(id="content").i.text.split(",")  # type: ignore
            assert len(authors) > 0, "Failed to find authors in the provided URL. The HTML structure has changed."
            return [author.strip() for author in authors]
        except Exception:
            logger.exception("Failed to extract authors from the provided URL. The HTML structure may have changed.")
            return None
