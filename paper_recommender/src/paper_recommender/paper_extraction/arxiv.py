"""
Implementation of the Arxiv class to extract paper information from arxiv.

Thank you to arXiv for use of its open access interoperability.
"""

import logging
import re
from urllib.parse import urlparse
from typing import Union
import feedparser
from feedparser import FeedParserDict
from .base import PaperExtractionBase, Paper

logger = logging.getLogger(__name__)


class Arxiv(PaperExtractionBase):
    """Extract paper information from arxiv."""

    DOMAIN = "arxiv.org"

    @staticmethod
    def extract_from_url(url: str) -> Union[Paper, None]:
        """Extract paper information from arxiv. Must return base.Paper dataclass.

        Args:
            url (str): URL of the paper to be extracted.

        Returns:
            Paper dataclass from base.py. Or None if any error or exception occured.

        NOTE: We assume that the provided URL is an arxiv URL. It is the responsibility of the caller to
        make sure this is the case. However this class does provide a method to check this assumption.
        """
        paper_id = Arxiv._extract_paper_id_from_url(url)
        if not paper_id:
            logger.error("Failed to extract paper id from the provided URL: %s", url)
            return None

        response = Arxiv.arxiv_api_id(paper_id)
        if response:
            response = response.entries[0]
        else:
            return None
        paper = Paper(
            url=response.link,
            title=response.title,
            authors=[author.name for author in response.authors],
            abstract=response.summary,
        )
        return paper

    @staticmethod
    def arxiv_api_id(paper_id: str) -> Union[FeedParserDict, None]:
        """Retriving paper information using arxiv API with the provided paper id.

        Args:
            paper_id: arxiv id of the paper to be retrieved.

        Returns:
            A feedparser.util.FeedParserDict containing the API response.
            If the paper id is invalid or other exception occured, return None.
        """
        try:
            return feedparser.parse(f"http://export.arxiv.org/api/query?id_list={paper_id}")
        except Exception:
            logger.exception(
                "Something went wrong while retrieving paper with the provided URL. Likely that the "
                "paper id is incorrect or the arxiv API has changed."
            )
            return None

    @staticmethod
    def _extract_paper_id_from_url(url: str) -> Union[str, None]:
        """Extract paper id from arxiv url.

        NOTE: findall will return a list containing all matches.
        However, the URL should have been validated before reaching this function;
        a valid arxiv URL should only have one match

        Args:
            url: URL of the paper to be extracted.

        Returns:
            A string containing the paper id. If fails to extract arxiv id, return None.
        """
        matches = re.findall(r"\d+\.\d+", url)
        if matches:
            return matches[0]
        else:
            return None

    @staticmethod
    def _arxiv_domain_check(url: str) -> bool:
        """Check if the provided url is from arxiv.

        Args:
            url: a URL string.

        Returns:
            True if the provided url is from arxiv. False otherwise.
        """
        parsed_url = urlparse(url)
        return parsed_url.netloc == Arxiv.DOMAIN
