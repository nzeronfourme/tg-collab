"""Shared polite HTTP client: per-host pacing, retries, and secret redaction."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
SENSITIVE_PARAMS = frozenset({"token", "api_hash", "access_token"})
MAX_RETRY_AFTER = 60.0


def redact_url(url: str) -> str:
    """Render sensitive query values as ``***`` and keep everything else intact."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    rendered: list[str] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_PARAMS:
            rendered.append(f"{quote(key, safe='')}=***")
        else:
            rendered.append(f"{quote(key, safe='')}={quote(value, safe='')}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(rendered), parts.fragment))


class HttpError(RuntimeError):
    """A request failed; the message never contains a raw secret query value."""

    def __init__(self, status_code: int, url: str, detail: str = "") -> None:
        self.status_code = status_code
        self.url = redact_url(url)
        message = f"HTTP {status_code} for {self.url}" if status_code else f"request failed for {self.url}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after.strip()), 0.0), MAX_RETRY_AFTER)
        except ValueError:
            pass
    return min(0.5 * (2**attempt), 10.0)


class HttpClient:
    """Small wrapper over httpx.Client used by every provider.

    ``sleep`` and ``clock`` are injectable so tests never sleep for real.
    """

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        user_agent: str = "tg-collab/0.1.0",
        delay_ms: int = 1000,
        timeout: float = 20,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        proxy: str | None = None,
    ) -> None:
        self._client = httpx.Client(
            transport=transport,
            proxy=None if transport is not None else proxy,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )
        self._transport = transport
        self._user_agent = user_agent
        self._timeout = timeout
        self._delay = max(delay_ms, 0) / 1000.0
        self._retries = max(int(retries), 0)
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    def without_proxy(self) -> HttpClient:
        """A direct client sharing this one's transport and pacing settings.

        httpx fixes the proxy per client, so a loopback SearXNG behind an
        upstream proxy needs its own client to be reachable.
        """
        return HttpClient(
            transport=self._transport,
            user_agent=self._user_agent,
            delay_ms=int(self._delay * 1000),
            timeout=self._timeout,
            retries=self._retries,
            sleep=self._sleep,
            clock=self._clock,
            proxy=None,
        )

    def _throttle(self, url: str, delay_ms: int | None = None) -> None:
        host = urlsplit(url).netloc.lower()
        delay = self._delay if delay_ms is None else max(int(delay_ms), 0) / 1000.0
        with self._lock:
            last = self._last_request.get(host)
            now = self._clock()
            if last is not None:
                remaining = delay - (now - last)
                if remaining > 0:
                    self._sleep(remaining)
                    now = self._clock()
            self._last_request[host] = now

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        delay_ms: int | None = None,
    ) -> httpx.Response:
        """GET with pacing and retries; returns the final (redirected) response."""
        request_url = str(httpx.URL(url, params=params)) if params else url
        last_status = 0
        for attempt in range(self._retries + 1):
            self._throttle(url, delay_ms)
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError:
                if attempt >= self._retries:
                    raise HttpError(0, request_url, "network error") from None
                self._sleep(min(2.0**attempt, 10.0))
                continue
            if response.status_code < 400:
                return response
            if response.status_code not in RETRY_STATUS:
                raise HttpError(response.status_code, request_url)
            last_status = response.status_code
            if attempt < self._retries:
                self._sleep(_retry_delay(response.headers.get("Retry-After"), attempt))
        raise HttpError(last_status, request_url)

    def get_text(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        return self.get(url, params=params, headers=headers).text

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        delay_ms: int | None = None,
    ) -> Any:
        response = self.get(url, params=params, headers=headers, delay_ms=delay_ms)
        try:
            return response.json()
        except ValueError:
            raise HttpError(
                response.status_code,
                str(httpx.URL(url, params=params)) if params else url,
                "invalid JSON response",
            ) from None
