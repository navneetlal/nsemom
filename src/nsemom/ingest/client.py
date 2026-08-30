"""Rate-limited, retrying HTTP client for NSE archive endpoints.

NSE drops requests carrying a non-browser user agent at the edge - a default
requests/curl UA yields a dropped connection rather than an error page. A
realistic UA is sufficient for nsearchives.nseindia.com; the cookie handshake
against www.nseindia.com is only needed for the /api/ endpoints, which the
bhavcopy path does not use.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass
from typing import Literal

import requests

from ..config import IngestConfig

log = logging.getLogger(__name__)

# 404 is a definitive "no file for this date" - a market holiday - and must never
# be retried. Everything here is transient and worth backing off on.
RETRYABLE_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


class FetchError(RuntimeError):
    """Raised when a URL still fails after the configured retries."""


@dataclass(frozen=True)
class FetchResult:
    status: Literal["ok", "not_found"]
    content: bytes | None = None
    sha256: str | None = None


class NseClient:
    def __init__(self, cfg: IngestConfig) -> None:
        self._cfg = cfg
        self._last_request_at = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": cfg.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": cfg.referer,
        })

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "NseClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_request_at
        remaining = self._cfg.min_request_interval_seconds - gap
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int) -> float:
        delay = self._cfg.backoff_base_seconds * (2 ** attempt)
        delay = min(delay, self._cfg.backoff_max_seconds)
        return delay * (0.5 + random.random() / 2)  # jitter, to avoid lockstep retries

    def bootstrap_session(self) -> None:
        """Obtain the cookies the www.nseindia.com/api/ endpoints require.

        The archive host needs no cookie - a realistic UA is enough - but the
        JSON API rejects a cookieless request. Loading a normal quote page first
        is what a browser does and what sets the cookies.
        """
        try:
            self._throttle()
            self._session.get(
                "https://www.nseindia.com/get-quotes/equity?symbol=TCS",
                timeout=self._cfg.request_timeout_seconds,
                headers={"Accept": "text/html,application/xhtml+xml",
                         "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"},
            )
        except requests.RequestException as exc:
            log.warning("session bootstrap failed (continuing): %s", exc)

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        last_reason = "unknown"

        for attempt in range(self._cfg.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(
                    url, timeout=self._cfg.request_timeout_seconds, headers=headers
                )
            except requests.RequestException as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return FetchResult(
                        status="ok",
                        content=resp.content,
                        sha256=hashlib.sha256(resp.content).hexdigest(),
                    )
                if resp.status_code == 404:
                    return FetchResult(status="not_found")
                last_reason = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise FetchError(f"{url}: {last_reason}")

            if attempt < self._cfg.max_retries:
                delay = self._backoff(attempt)
                log.warning(
                    "fetch failed (%s), retry %d/%d in %.1fs: %s",
                    last_reason, attempt + 1, self._cfg.max_retries, delay, url,
                )
                time.sleep(delay)

        raise FetchError(
            f"{url}: giving up after {self._cfg.max_retries} retries ({last_reason})"
        )
