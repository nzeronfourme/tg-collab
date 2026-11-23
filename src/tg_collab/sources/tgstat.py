"""TGStat sources: official API (token-first) and a public best-effort probe."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from tg_collab.config import get_bool, get_setting
from tg_collab.http_client import HttpError
from tg_collab.models import Candidate, ChannelPage
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.tg_public import parse_count

API_BASE = "https://api.tgstat.ru"
PUBLIC_BASE = "https://tgstat.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
WALL_MARKER = "Authentication Required"


class TgstatError(RuntimeError):
    """A TGStat API error; the message never contains the token."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        text = f"TGStat API error: {code}"
        if message:
            text = f"{text} ({message})"
        super().__init__(text)


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;\n]", value) if part.strip()]


def _mark_status(ctx: ProviderContext | None, status: str) -> None:
    """Publish the truthful end status, so the count fallback cannot call it ``empty``."""
    if ctx is not None:
        ctx.statuses["tgstat"] = status


def _status_for_error(exc: TgstatError) -> str:
    if exc.code.startswith("quota_"):
        return "quota"
    if exc.code == "no_active_subscription":
        return "blocked"
    if exc.code.startswith("flood_control"):
        return "quota"
    if exc.code == "disabled":
        return "disabled"
    return "error"


def _status_for_http(exc: HttpError) -> str:
    if exc.status_code == 429:
        return "quota"
    if exc.status_code == 403:
        return "blocked"
    return "error"


class TgstatClient:
    def __init__(self, http: Any, token: str, sleep: Any = time.sleep) -> None:
        self.http = http
        self.token = token
        self._sleep = sleep
        self.disabled = False
        self.warnings: list[str] = []

    def _warn(self, code: str) -> None:
        message = f"tgstat: {code}"
        if message not in self.warnings:
            self.warnings.append(message)

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.disabled:
            raise TgstatError("disabled")
        query = {"token": self.token, **params}
        for attempt in range(2):
            payload = self.http.get_json(API_BASE + path, params=query)
            if str(payload.get("status")) == "ok":
                return payload.get("response") or {}
            code = str(payload.get("error") or "unknown_error")
            detail = str(payload.get("message") or "")
            if self.token:
                detail = detail.replace(self.token, "***")
            if code.startswith("flood_control"):
                self._warn(code)
                if attempt == 0:
                    self._sleep(60 if code.endswith("60") else 10)
                    continue
                raise TgstatError(code, detail)
            if code.startswith("quota_") or code == "no_active_subscription":
                self.disabled = True
                self._warn(code)
                raise TgstatError(code, detail)
            if code == "outdated_statistics":
                self._warn(code)
                raise TgstatError(code, detail)
            raise TgstatError(code, detail)
        raise TgstatError("flood_control")

    def search(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        country: str,
        limit: int = 100,
        peer_type: str = "channel",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "country": country,
            "limit": limit,
            "peer_type": peer_type,
        }
        if q:
            params["q"] = q
        if category:
            params["category"] = category
        return self._request("/channels/search", params)

    def get(self, channel_id: str) -> dict[str, Any]:
        return self._request("/channels/get", {"channelId": channel_id})

    def stat(self, channel_id: str) -> dict[str, Any]:
        return self._request("/channels/stat", {"channelId": channel_id})


class TgstatDiscovery:
    name = "tgstat"

    def __init__(self, settings: dict[str, str], http: Any) -> None:
        self.settings = settings
        self.http = http
        self.token = get_setting(settings, "tgstat_token").strip()
        self.client = TgstatClient(http, self.token) if self.token else None
        self._enabled = get_bool(settings, "sources_tgstat")
        self._warned = False

    @property
    def warnings(self) -> list[str]:
        return self.client.warnings if self.client is not None else []

    def enabled(self, settings: dict[str, str]) -> bool:
        return self._enabled

    def _warn(self, ctx: ProviderContext | None, message: str) -> None:
        if message not in self.warnings and self.client is not None:
            self.client.warnings.append(message)
        if ctx is not None and message not in ctx.warnings:
            ctx.warnings.append(message)

    def discover(self, ctx: ProviderContext) -> list[Candidate]:
        if not self._enabled:
            _mark_status(ctx, "disabled")
            return []
        if self.client is None:
            if not self._warned:
                self._warned = True
                self._warn(ctx, "tgstat: set tgstat_token to use the TGStat API")
            _mark_status(ctx, "no_key")
            return []
        queries = _split_list(get_setting(self.settings, "tgstat_queries"))
        categories = _split_list(get_setting(self.settings, "tgstat_categories"))
        country = get_setting(self.settings, "tgstat_country").strip()
        if not country or not (queries or categories):
            if not self._warned:
                self._warned = True
                self._warn(
                    ctx,
                    "tgstat: set tgstat_country and tgstat_queries or tgstat_categories to search",
                )
            _mark_status(ctx, "disabled")
            return []

        found: dict[str, Candidate] = {}
        requests_ok = 0
        failure: str | None = None
        try:
            for query in queries:
                self._collect(found, self.client.search(q=query, country=country))
                requests_ok += 1
            for category in categories:
                self._collect(found, self.client.search(category=category, country=country))
                requests_ok += 1
        except TgstatError as exc:
            self._warn(ctx, str(exc))
            failure = _status_for_error(exc)
        except HttpError as exc:
            detail = exc.status_code or "network"
            self._warn(ctx, f"tgstat: request failed ({detail})")
            failure = _status_for_http(exc)
        if found:
            _mark_status(ctx, "ok")
        elif requests_ok:
            _mark_status(ctx, "empty")
        else:
            _mark_status(ctx, failure or "error")
        return list(found.values())

    def _collect(self, found: dict[str, Candidate], response: dict[str, Any]) -> None:
        for item in response.get("items") or []:
            username = str(item.get("username") or "").lstrip("@")
            if not username or username.lower() in found:
                continue
            found[username.lower()] = Candidate(
                username=username,
                source="tgstat",
                title=item.get("title"),
                description=item.get("about"),
                subscribers=item.get("participants_count"),
                meta={"tgstat": item},
            )


class TgstatEnrichment:
    name = "tgstat"

    def __init__(self, settings: dict[str, str], http: Any) -> None:
        token = get_setting(settings, "tgstat_token").strip()
        self.client = TgstatClient(http, token) if token else None
        self._enabled = get_bool(settings, "sources_tgstat") and bool(token)

    @property
    def warnings(self) -> list[str]:
        return self.client.warnings if self.client is not None else []

    def enabled(self, settings: dict[str, str]) -> bool:
        return self._enabled

    def enrich(self, username: str) -> ChannelPage | None:
        if self.client is None:
            return None
        try:
            details = self.client.stat(f"@{username}")
        except TgstatError:
            return None
        except HttpError as exc:
            self.client.warnings.append(f"tgstat: request failed ({exc.status_code})")
            return None
        if not details.get("participants_count"):
            try:
                info = self.client.get(f"@{username}")
            except (TgstatError, HttpError):
                info = {}
            details = {**info, **details}
        tgstat = {
            key: details.get(key)
            for key in ("participants_count", "avg_post_reach", "err_percent", "ci_index")
            if details.get(key) is not None
        }
        return ChannelPage(
            username=username,
            subscribers=details.get("participants_count"),
            tgstat=tgstat or None,
        )


def _labeled_number(text: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}[^\d]{{0,24}}(\d[\d\s.,]*)\s*(тыс\.?|млн\.?|k|m)?",
            text,
            re.IGNORECASE,
        )
        if match is None:
            continue
        value = match.group(1).strip()
        suffix = (match.group(2) or "").lower().rstrip(".")
        if suffix:
            return float(parse_count(f"{value}{suffix}") or 0)
        cleaned = value.replace(" ", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _parse_card(html: str, username: str) -> ChannelPage:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    subscribers = _labeled_number(text, ("Подписчики", "Subscribers"))
    reach = _labeled_number(text, ("Средний охват", "Average reach", "Avg reach"))
    err = _labeled_number(text, ("ERR",))
    tgstat = {}
    if reach is not None:
        tgstat["avg_post_reach"] = int(round(reach))
    if err is not None:
        tgstat["err_percent"] = err
    return ChannelPage(
        username=username,
        subscribers=int(round(subscribers)) if subscribers is not None else None,
        tgstat=tgstat or None,
    )


def _parse_public_search(html: str) -> list[Candidate]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, Candidate] = {}
    for anchor in soup.select('a[href*="/channel/@"]'):
        match = re.search(r"/channel/@([A-Za-z0-9_]{4,32})", str(anchor.get("href")))
        if match is None:
            continue
        username = match.group(1)
        found.setdefault(
            username.lower(),
            Candidate(username=username, source="tgstat", title=anchor.get_text(" ", strip=True) or None),
        )
    return list(found.values())


class TgstatPublicProvider:
    """Best-effort public pages. Currently login-walled, so it degrades to one warning."""

    name = "tgstat"

    def __init__(self, settings: dict[str, str], http: Any) -> None:
        self.settings = settings
        self.http = http
        self.token = get_setting(settings, "tgstat_token").strip()
        self._enabled = get_bool(settings, "sources_tgstat") and not self.token
        self.warnings: list[str] = []
        self._walled = False
        self._warned_setup = False
        self._failure: str | None = None

    def enabled(self, settings: dict[str, str]) -> bool:
        return self._enabled

    def _wall(self) -> None:
        self._walled = True
        message = "tgstat_auth_required: TGStat public pages require login; set tgstat_token to use the API"
        if message not in self.warnings:
            self.warnings.append(message)

    def _get(self, url: str) -> str | None:
        if self._walled:
            return None
        try:
            response = self.http.get(url, headers={"User-Agent": BROWSER_UA})
        except HttpError as exc:
            if exc.status_code == 429:
                self._failure = "quota"
                self._wall()
            else:
                self._failure = "blocked" if exc.status_code == 403 else "error"
                self.warnings.append(f"tgstat public probe failed: HTTP {exc.status_code}")
            return None
        if WALL_MARKER in response.text:
            self._failure = "blocked"
            self._wall()
            return None
        return response.text

    def _surface(self, ctx: ProviderContext | None) -> None:
        if ctx is None:
            return
        for warning in self.warnings:
            if warning not in ctx.warnings:
                ctx.warnings.append(warning)

    def discover(self, ctx: ProviderContext) -> list[Candidate]:
        if not self._enabled:
            _mark_status(ctx, "disabled")
            return []
        queries = _split_list(get_setting(self.settings, "tgstat_queries"))
        country = get_setting(self.settings, "tgstat_country").strip()
        if not queries or not country:
            if not self._warned_setup:
                self._warned_setup = True
                self.warnings.append(
                    "tgstat public discovery needs tgstat_country and tgstat_queries"
                )
            self._surface(ctx)
            # Nothing is configured; a token is the documented fix for this source.
            _mark_status(ctx, "no_key" if not self.token else "disabled")
            return []
        found: dict[str, Candidate] = {}
        pages_ok = 0
        for query in queries:
            html = self._get(
                f"{PUBLIC_BASE}/channels/search?q={quote(query)}&country={quote(country)}"
            )
            if html is None:
                break
            pages_ok += 1
            for candidate in _parse_public_search(html):
                found.setdefault(candidate.username.lower(), candidate)
        self._surface(ctx)
        if found:
            status = "ok"
        elif pages_ok:
            status = "empty"
        elif self._failure == "quota":
            status = "quota"
        elif self._failure == "blocked":
            status = "blocked"
        elif not self.token:
            # The public pages are login-walled; without a token no request can succeed.
            status = "no_key"
        else:
            status = self._failure or "error"
        _mark_status(ctx, status)
        return list(found.values())

    def enrich(self, username: str) -> ChannelPage | None:
        if not self._enabled:
            return None
        html = self._get(f"{PUBLIC_BASE}/channel/@{username}")
        if html is None:
            return None
        return _parse_card(html, username)


def build_discovery_provider(settings: dict[str, str], http: Any) -> Any:
    """Token-first policy: the API when a token exists, the public probe otherwise."""
    if get_setting(settings, "tgstat_token").strip():
        return TgstatDiscovery(settings, http)
    return TgstatPublicProvider(settings, http)


def build_enrichment_provider(settings: dict[str, str], http: Any) -> Any:
    if get_setting(settings, "tgstat_token").strip():
        return TgstatEnrichment(settings, http)
    return TgstatPublicProvider(settings, http)
