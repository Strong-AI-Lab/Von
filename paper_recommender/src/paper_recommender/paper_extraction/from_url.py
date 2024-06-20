"""This module provides functions to extract paper abstract from a given URL."""

import logging
from urllib.parse import urlparse
from typing import Union
import requests
from .base import Paper

# Added paper sources should be imported below
from .arxiv import Arxiv
from .nature import Nature
from .aaai import AAAI
from .ijcai import IJCAI
from .neurips import NeurIPS
from .acl_anthology import ACLAnthology
from .pmlr import PMLR
from .jmlr import JMLR

logger = logging.getLogger(__name__)

# A dictionary mapping domain names to their corresponding class that implements base.PaperExtractionBase
# This is used to dynamically call the correct class based on the domain name of the URL
# This needs to be updated as new sources are added
DOMAINS = {
    Arxiv.DOMAIN: Arxiv,
    Nature.DOMAIN: Nature,
    AAAI.DOMAIN: AAAI,
    IJCAI.DOMAIN: IJCAI,
    NeurIPS.DOMAIN: NeurIPS,
    ACLAnthology.DOMAIN: ACLAnthology,
    PMLR.DOMAIN: PMLR,
    JMLR.DOMAIN: JMLR,
}


def known_domain(url: str, file_path: str) -> bool:
    """Check if the domain of the provided URL is known.

    Args:
        url: URL of the paper.
        file_path: The file path to log the unknown domains.
    Returns:
        True if the domain is known. False otherwise.
    """
    try:
        domain = urlparse(url).netloc
    except Exception:
        logger.exception("An error occurred while parsing the provided URL: %s.", url)
        return False
    if domain not in DOMAINS:
        logger.warning("The domain contained in the provided URL is not currently supported: %s.", url)
        # log the unknown domain
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(f"{domain},{url}\n")
        return False
    else:
        return True


def extract_abstract_from_url(url: str) -> Union[Paper, None]:
    """Extract paper abstract from the provided URL.

    Args:
        url: URL of the paper to be extracted.

    Returns:
        Paper dataclass. Or None if any error or exception occured.
    """
    try:
        domain = urlparse(url).netloc
    except Exception:
        logger.exception("An error occurred while parsing the provided URL: %s.", url)
        return None
    if not url_live(url):
        return None
    else:
        try:
            return DOMAINS[domain].extract_from_url(url)
        except Exception:
            logger.exception("Failed to extract paper information from the provided URL: %s.", url)
            return None


def url_live(url: str) -> bool:
    """Tests if the url is accessible.

    Args:
        url: URL in string.

    Returns:
        True if the URL is accessible. False otherwise.
    """
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return True
        else:
            logger.error("Provided URL is not accessible: %s.", url)
            return False
    except Exception:
        logger.exception("Failed to access the provided URL: %s.", url)
        return False
