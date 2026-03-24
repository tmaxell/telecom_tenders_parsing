"""HTTP-клиент для OCDS API с поддержкой пагинации, ретраев и rate-limiting."""

from __future__ import annotations

import time
import logging
from typing import Generator, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


class OCDSApiClient:
    """Generic OCDS release-package fetcher."""

    def __init__(
        self,
        base_url: str,
        timeout: int = 45,
        max_retries: int = 3,
        delay: float = 1.5,
        headers: Optional[dict] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "OCDS-TenderMonitor/1.0",
        })
        if headers:
            self.session.headers.update(headers)

    # ---- low-level --------------------------------------------------------
    def _get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    wait = min(120, 2 ** attempt * self.delay)
                    logger.warning("Rate-limited (%s). Sleeping %.1fs", url, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                time.sleep(self.delay)
                return resp.json()
            except requests.HTTPError as exc:
                if resp.status_code >= 500:
                    logger.warning("Server %d on %s (attempt %d)", resp.status_code, url, attempt)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("HTTP %d: %s — %s", resp.status_code, url, exc)
                    return None
            except requests.RequestException as exc:
                logger.warning("Request error %s (attempt %d): %s", url, attempt, exc)
                time.sleep(2 ** attempt)
        logger.error("Exhausted %d retries for %s", self.max_retries, url)
        return None

    # ---- high-level -------------------------------------------------------
    def fetch_packages(
        self,
        path: str = "/releases.json",
        params: Optional[dict] = None,
        max_pages: int = 200,
    ) -> Generator[dict, None, None]:
        """Yield release-package dicts, following pagination."""
        url = f"{self.base_url}{path}"
        page = 0
        while url and page < max_pages:
            data = self._get(url, params)
            if not data:
                break
            yield data
            page += 1
            # стандартная OCDS-пагинация
            url = None
            params = None
            links = data.get("links", {})
            if isinstance(links, dict):
                url = links.get("next")
            if not url:
                url = data.get("next")
            if not url:
                url = data.get("next_page_url")
            if url:
                logger.debug("Page %d done, next → %s", page, url[:120])

    def iter_releases(
        self,
        path: str = "/releases.json",
        params: Optional[dict] = None,
        max_pages: int = 200,
        tag_filter: Optional[set[str]] = None,
    ) -> Generator[dict, None, None]:
        """Yield individual OCDS releases."""
        tag_filter = tag_filter or {"tender"}
        for pkg in self.fetch_packages(path, params, max_pages):
            for release in pkg.get("releases", []):
                tags = release.get("tag", [])
                if isinstance(tags, str):
                    tags = [tags]
                if tag_filter and not tag_filter.intersection(tags):
                    continue
                yield release

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()