import base64
import os
import typing as t

import requests


class JiraConfigurationError(RuntimeError):
    pass


def _get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise JiraConfigurationError(f"Missing required environment variable: {name}")
    return value


def _build_basic_auth_header(email: str, token: str) -> str:
    auth_bytes = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(auth_bytes).decode("utf-8")


class JiraClient:
    """Minimal Jira REST client using API token basic auth.

    Expects the following environment variables to be set:
      - ATLASSIAN_API_EMAIL
      - ATLASSIAN_API_TOKEN
      - ATLASSIAN_SITE_BASE (e.g. https://example.atlassian.net)
    """

    def __init__(
        self,
        site_base: t.Optional[str] = None,
        email: t.Optional[str] = None,
        token: t.Optional[str] = None,
    ):
        self.site_base = (
            site_base or os.environ.get("ATLASSIAN_SITE_BASE", "")
        ).rstrip("/")
        self.email = email or os.environ.get("ATLASSIAN_API_EMAIL", "")
        self.token = token or os.environ.get("ATLASSIAN_API_TOKEN", "")
        if not (self.site_base and self.email and self.token):
            pass
        self._session = requests.Session()
        if self.email and self.token:
            self._session.headers["Authorization"] = _build_basic_auth_header(
                self.email, self.token
            )
        self._session.headers.setdefault("Accept", "application/json")

    def _ensure_config(self):
        if not (self.site_base and self.email and self.token):
            missing = [
                n
                for n, v in [
                    ("ATLASSIAN_SITE_BASE", self.site_base),
                    ("ATLASSIAN_API_EMAIL", self.email),
                    ("ATLASSIAN_API_TOKEN", self.token),
                ]
                if not v
            ]
            raise JiraConfigurationError(
                "Missing Jira configuration: " + ", ".join(missing)
            )

    def get_issue(self, key: str) -> dict:
        self._ensure_config()
        url = f"{self.site_base}/rest/api/3/issue/{key}"
        resp = self._session.get(url, timeout=30)
        if resp.status_code == 401:
            raise JiraConfigurationError("Unauthorized: check email/token validity")
        if resp.status_code == 403:
            raise JiraConfigurationError(
                "Forbidden: account lacks permission to view issue"
            )
        resp.raise_for_status()
        return resp.json()

    def search(
        self,
        jql: str,
        fields: t.Optional[t.List[str]] = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> dict:
        self._ensure_config()
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "startAt": start_at,
        }
        if fields:
            payload["fields"] = fields
        url = f"{self.site_base}/rest/api/3/search"
        resp = self._session.post(url, json=payload, timeout=60)
        if resp.status_code == 401:
            raise JiraConfigurationError("Unauthorized: check email/token validity")
        resp.raise_for_status()
        return resp.json()


_singleton: t.Optional[JiraClient] = None


def get_client() -> JiraClient:
    global _singleton
    if _singleton is None:
        _singleton = JiraClient()
    return _singleton


def fetch_issue(key: str) -> dict:
    return get_client().get_issue(key)
