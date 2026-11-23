"""HTML search engines as a zero-key discovery source for ``t.me`` channels."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from tg_collab.config import (
    SETTING_DEFAULTS,
    get_bool,
    get_int,
    get_setting,
    resolve_search_engines,
)
from tg_collab.http_client import HttpError
from tg_collab.models import Candidate
from tg_collab.similarity import niche_tags, profile_posts, tokenize
from tg_collab.sources.base import ProviderContext, SearchEngine
from tg_collab.sources.tg_public import (
    is_discoverable_username,
    ordered_telegram_usernames,
)
from tg_collab.store import get_search_cache, put_search_cache

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
ENGINE_URLS = {
    "yandex": "https://yandex.ru/search/",
    "google": "https://www.google.com/search",
    "duckduckgo": "https://html.duckduckgo.com/html/",
    "mojeek": "https://www.mojeek.com/search",
    "bing": "https://www.bing.com/search",
    "yahoo": "https://search.yahoo.com/search",
    "startpage": "https://www.startpage.com/sp/search",
}
CAPTCHA_MARKERS = {
    "yandex": ("showcaptcha", "checkcaptcha", "подтвердите, что запросы"),
    "google": ("unusual traffic", "our systems have detected", "/sorry/", "recaptcha"),
    "duckduckgo": ("anomaly-modal", "captcha"),
    "mojeek": ("captcha", "unusual traffic", "rate limit", "too many requests"),
    "lyzem": ("captcha", "unusual traffic", "recaptcha"),
    "bing": ("captcha", "unusual traffic", "verify you are a human", "one last step"),
    "yahoo": ("captcha", "unusual traffic", "verify you are a human"),
    "startpage": ("captcha", "unusual traffic", "verify you are a human", "anomaly"),
}
#: Engines that need a key, a self-hosted URL or an explicit toggle. They are
#: known names now, so a configured-but-unavailable one is reported with a
#: status instead of being treated as a typo.
OPTIONAL_ENGINES = frozenset({"brave", "searxng", "lyzem"})
OPTIONAL_ENGINE_STATUS = {"brave": "no_key", "searxng": "disabled", "lyzem": "disabled"}
#: One-line fixes shown next to a status when the engine is named but unusable.
OPTIONAL_ENGINE_REMEDY = {
    "brave": "search: brave skipped; set brave_search_api_key in settings to enable it",
}


class CaptchaError(RuntimeError):
    """The engine answered with a CAPTCHA or an anti-bot page."""


class EngineResultError(RuntimeError):
    """The engine answered, but its payload marks the search itself as failed."""


class HtmlSearchEngine:
    max_queries = 0

    def __init__(self, name: str, http: Any, region: str, delay_ms: int) -> None:
        self.name = name
        self.http = http
        self.region = region
        self.delay_ms = delay_ms

    @property
    def cache_key(self) -> str:
        return self.name

    def _params(self, query: str, limit: int) -> dict[str, Any]:
        if self.name == "google":
            params: dict[str, Any] = {"q": query, "num": limit}
            if self.region:
                params["gl"] = self.region
            return params
        if self.name == "mojeek":
            return {"q": query}
        if self.name == "duckduckgo":
            params = {"q": query}
            if self.region and len(self.region) == 2:
                params["kl"] = f"{self.region}-{self.region}"
            return params
        if self.name == "bing":
            params = {"q": query}
            if len(self.region) == 2:
                params["cc"] = self.region.upper()
            return params
        if self.name == "yahoo":
            return {"p": query}
        if self.name == "startpage":
            return {"query": query}
        return {"text": query}

    def search(self, query: str, limit: int) -> list[str]:
        response = self.http.get(
            ENGINE_URLS[self.name],
            params=self._params(query, limit),
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "ru,en;q=0.8"},
            delay_ms=self.delay_ms,
        )
        html = response.text
        lowered = unquote(html).lower()
        markers = CAPTCHA_MARKERS[self.name]
        if any(marker in lowered for marker in markers) or "/showcaptcha" in str(response.url):
            raise CaptchaError(self.name)
        return extract_candidates(html, limit)


EngineFactory = Callable[[str, Any, str, int, dict[str, str]], SearchEngine | None]


def _html_engine(
    name: str, http: Any, region: str, delay_ms: int, settings: dict[str, str]
) -> HtmlSearchEngine:
    return HtmlSearchEngine(name, http, region, delay_ms)


ENGINE_FACTORIES: dict[str, EngineFactory] = {name: _html_engine for name in ENGINE_URLS}


def register_engine(name: str, factory: EngineFactory) -> None:
    ENGINE_FACTORIES[name] = factory


def known_engines() -> frozenset[str]:
    """Live view of the registry, so a later ``register_engine`` is not "unknown"."""
    return frozenset(ENGINE_FACTORIES) | OPTIONAL_ENGINES


def build_engine(
    name: str,
    http: Any,
    region: str,
    delay_ms: int,
    settings: dict[str, str] | None = None,
) -> SearchEngine | None:
    factory = ENGINE_FACTORIES.get(name)
    if factory is None:
        return None
    return factory(name, http, region, delay_ms, settings or {})


BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_COUNT = 20
LYZEM_URL = "https://lyzem.com/search"
LYZEM_MAX_PER_PAGE = 50
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class EngineConfigError(ValueError):
    """An optional engine has a configured value that cannot be used."""


def validate_searxng_url(value: str) -> str:
    """Return the normalized base URL; raise ``EngineConfigError`` otherwise.

    Shares the user-facing reason with the settings layer (todo 5).
    """
    parts = urlsplit((value or "").strip())
    if parts.scheme not in {"http", "https"}:
        raise EngineConfigError(
            "searxng needs a full http(s) URL, for example http://127.0.0.1:8080"
        )
    if not parts.hostname:
        raise EngineConfigError("searxng URL has no hostname")
    if parts.username or parts.password:
        raise EngineConfigError("searxng URL must not contain credentials")
    try:
        # Reading .port validates the number; the value is not kept.
        _ = parts.port
    except ValueError as exc:
        raise EngineConfigError(f"searxng URL has an invalid port: {exc}") from None
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def parse_searxng_urls(value: str) -> list[str]:
    """One or more comma/newline-separated instance URLs, normalized in order."""
    urls: list[str] = []
    for raw in re.split(r"[,\n]+", value or ""):
        if not raw.strip():
            continue
        url = validate_searxng_url(raw)
        if url not in urls:
            urls.append(url)
    return urls


def _is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS


def _json_urls(payload: Any, *keys: str) -> list[str]:
    node: Any = payload
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, list):
        return []
    return [
        item["url"]
        for item in node
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]


def _usernames_from_urls(urls: list[str], limit: int) -> list[str]:
    return ordered_telegram_usernames(" ".join(urls))[: max(limit, 0)]


class BraveSearchEngine:
    """Official Brave Search API; answers 401/429 on a key or quota problem."""

    name = "brave"
    max_queries = 0

    def __init__(self, api_key: str, http: Any, region: str, delay_ms: int) -> None:
        self.api_key = api_key
        self.http = http
        self.region = region
        self.delay_ms = delay_ms

    @property
    def cache_key(self) -> str:
        return self.name

    def search(self, query: str, limit: int) -> list[str]:
        params: dict[str, Any] = {"q": query, "count": min(max(limit, 1), BRAVE_MAX_COUNT)}
        if len(self.region) == 2:
            # Brave wants an uppercase 2-letter region such as ``RU``.
            params["country"] = self.region.upper()
        payload = self.http.get_json(
            BRAVE_URL,
            params=params,
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            delay_ms=self.delay_ms,
        )
        return _usernames_from_urls(_json_urls(payload, "web", "results"), limit)


def _engine_label(item: Any) -> str:
    if isinstance(item, (list, tuple)) and item:
        return str(item[0])
    if isinstance(item, dict):
        return str(item.get("engine") or item.get("name") or item)
    return str(item)


class SearxngSearchEngine:
    """Self-hosted SearXNG over its JSON API; one or more instance URLs.

    Instances are tried in order and the first usable answer wins; an empty
    answer is usable. When every instance fails, the last failure is raised so
    the provider can map it to the status contract.
    """

    name = "searxng"
    max_queries = 0

    def __init__(
        self,
        base_urls: list[str],
        http: Any,
        region: str,
        delay_ms: int,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.base_urls = base_urls
        self.base_url = base_urls[0]
        self.region = region
        self.delay_ms = delay_ms
        self._client_factory = client_factory
        self._remote = http
        self._direct: Any | None = None
        self.http = http
        if client_factory is not None and _is_loopback(self.base_url):
            self._direct = client_factory()
            self.http = self._direct

    @property
    def cache_key(self) -> str:
        return ",".join(self.base_urls)

    def _client_for(self, base: str) -> Any:
        # The dedicated direct client is closed after a run; a reused provider
        # gets a fresh one instead of a closed connection pool.
        if self._client_factory is None or not _is_loopback(base):
            return self._remote
        if self._direct is None or getattr(self._direct, "is_closed", True):
            self._direct = self._client_factory()
            self.http = self._direct
        return self._direct

    def close(self) -> None:
        """Release the direct client created for a loopback instance behind a proxy."""
        if self._direct is not None:
            self._direct.close()

    def _search_one(self, base: str, query: str, limit: int) -> list[str]:
        params: dict[str, Any] = {"q": query, "format": "json"}
        if self.region:
            params["language"] = self.region
        payload = self._client_for(base).get_json(
            f"{base}/search",
            params=params,
            headers={"Accept": "application/json"},
            delay_ms=self.delay_ms,
        )
        results = _json_urls(payload, "results")
        if not results and isinstance(payload, dict):
            unresponsive = payload.get("unresponsive_engines")
            if unresponsive:
                names = ", ".join(_engine_label(item) for item in unresponsive)
                raise EngineResultError(f"searxng reported unresponsive engines: {names}")
        return _usernames_from_urls(results, limit)

    def search(self, query: str, limit: int) -> list[str]:
        failure: Exception | None = None
        for base in self.base_urls:
            try:
                return self._search_one(base, query, limit)
            except (HttpError, EngineResultError) as exc:
                failure = exc
        if failure is not None:
            raise failure
        return []


class LyzemSearchEngine:
    """lyzem.com index; one query per run because it throttles aggressively."""

    name = "lyzem"
    max_queries = 1

    def __init__(self, http: Any, region: str, delay_ms: int) -> None:
        self.http = http
        self.region = region
        self.delay_ms = delay_ms

    @property
    def cache_key(self) -> str:
        return self.name

    def search(self, query: str, limit: int) -> list[str]:
        response = self.http.get(
            LYZEM_URL,
            params={
                "q": query,
                "f": "all",
                "per-page": min(max(limit, 1), LYZEM_MAX_PER_PAGE),
            },
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "ru,en;q=0.8"},
            delay_ms=self.delay_ms,
        )
        html = unquote(response.text)
        if any(marker in html.lower() for marker in CAPTCHA_MARKERS["lyzem"]):
            raise CaptchaError(self.name)
        names = ordered_telegram_usernames(html)
        return names[: max(limit, 0)]


def _brave_engine(
    name: str, http: Any, region: str, delay_ms: int, settings: dict[str, str]
) -> SearchEngine | None:
    key = get_setting(settings, "brave_search_api_key").strip()
    if not key:
        return None
    return BraveSearchEngine(key, http, region, delay_ms)


def _searxng_engine(
    name: str, http: Any, region: str, delay_ms: int, settings: dict[str, str]
) -> SearchEngine | None:
    raw = get_setting(settings, "searxng_url").strip()
    if not raw:
        return None
    bases = parse_searxng_urls(raw)
    factory: Callable[[], Any] | None = None
    if get_setting(settings, "proxy_url").strip() and any(_is_loopback(url) for url in bases):
        # An upstream proxy cannot dial the caller's loopback address.
        factory = http.without_proxy
    return SearxngSearchEngine(bases, http, region, delay_ms, client_factory=factory)


def _lyzem_engine(
    name: str, http: Any, region: str, delay_ms: int, settings: dict[str, str]
) -> SearchEngine | None:
    # Off unless ``sources_search_lyzem`` is set: no robots or ToS grant to
    # script it. A 404 on /robots.txt means "no directives", not permission.
    if not get_bool(settings, "sources_search_lyzem"):
        return None
    return LyzemSearchEngine(http, region, delay_ms)


ENGINE_FACTORIES.update(
    {"brave": _brave_engine, "searxng": _searxng_engine, "lyzem": _lyzem_engine}
)


def _unique(names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def extract_candidates(html: str, limit: int) -> list[str]:
    soup = BeautifulSoup(unquote(html), "lxml")
    hrefs = " ".join(str(anchor.get("href") or "") for anchor in soup.select("a[href]"))
    text = f"{hrefs} {soup.get_text(' ', strip=True)}"
    names = ordered_telegram_usernames(text)
    return names[: max(limit, 0)]


QUERY_LIMIT_DEFAULT = 10
QUERY_LIMIT_MAX = 12
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def clamp_max_queries(value: Any) -> int:
    """Clamp a query budget to 1..12; a non-numeric value uses the default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return QUERY_LIMIT_DEFAULT
    return min(max(number, 1), QUERY_LIMIT_MAX)


def _profile_texts(profile: Any) -> list[str]:
    texts = [profile.title or "", profile.description or ""]
    texts.extend(str(item.get("text") or "") for item in profile_posts(profile))
    return texts


def _quote_if_multiword(term: str) -> str:
    return f'"{term}"' if " " in term else term


def _query_families(profile: Any, texts: list[str]) -> list[list[str]]:
    """One queue per query family; all queues are visited in every round."""
    terms, tag_values, domain_values = niche_tags(profile)
    if not terms:
        seen: set[str] = set()
        for word in tokenize(f"{profile.title or ''} {profile.description or ''}"):
            if word not in seen:
                seen.add(word)
                terms.append(word)
    site = [f"site:t.me {_quote_if_multiword(term)}" for term in terms]
    telegram = [f'"{term}" telegram' for term in terms]
    families = [site, telegram]
    if any(CYRILLIC_RE.search(text) for text in texts):
        families.append([f'"{term}" телеграм' for term in terms])
    families.append([f"#{tag}" for tag in tag_values])
    families.append([f'"{domain}" telegram' for domain in domain_values])
    return [family for family in families if family]


def build_queries(profile: Any, max_queries: int) -> list[str]:
    """Family-based queries, interleaved so the budget covers several terms.

    Each round takes one query from every family, shifting the index by the
    family position, so the first six queries touch several families and
    several terms instead of spending the whole cap on the first term.
    """
    if profile is None:
        return []
    limit = clamp_max_queries(max_queries)
    families = _query_families(profile, _profile_texts(profile))
    if not families:
        return []
    queries: list[str] = []
    seen: set[str] = set()
    for round_index in range(max(len(family) for family in families)):
        for family_index, family in enumerate(families):
            query = family[(round_index + family_index) % len(family)]
            if query in seen:
                continue
            seen.add(query)
            queries.append(query)
            if len(queries) >= limit:
                return queries
    return queries


def _max_queries(settings: dict[str, str]) -> int:
    value = get_int(settings, "search_max_queries")
    return clamp_max_queries(value if value is not None else QUERY_LIMIT_DEFAULT)


def _max_results(settings: dict[str, str]) -> int:
    value = get_int(settings, "search_max_results")
    return min(max(value if value is not None else 20, 1), 50)


def _activated_optional(settings: dict[str, str]) -> list[str]:
    """Optional engines whose own credential is set, in a stable order."""
    names: list[str] = []
    if get_setting(settings, "brave_search_api_key").strip():
        names.append("brave")
    if get_setting(settings, "searxng_url").strip():
        names.append("searxng")
    if get_bool(settings, "sources_search_lyzem"):
        names.append("lyzem")
    return names


class WebSearchProvider:
    name = "web_search"

    def __init__(self, settings: dict[str, str], http: Any) -> None:
        self.settings = settings
        self.http = http
        self.warnings: list[str] = []
        self.disabled: set[str] = set()
        self.statuses: dict[str, str] = {}
        delay = get_int(settings, "search_delay_ms")
        delay = 2500 if delay is None else delay
        region = get_setting(settings, "search_region").strip().lower()
        self.engines: list[SearchEngine] = []
        configured = resolve_search_engines(get_setting(settings, "search_engines"))
        known = known_engines()
        for name in _unique(configured + _activated_optional(settings)):
            if name not in known:
                self._warn(f"search: unknown engine {name}")
                continue
            toggle = f"sources_search_{name}"
            if toggle in SETTING_DEFAULTS and not get_bool(settings, toggle):
                self.statuses[name] = "disabled"
                continue
            try:
                engine = build_engine(name, http, region, delay, settings)
            except EngineConfigError as exc:
                self.statuses[name] = "error"
                self._warn(f"search: {name}: {exc}")
                continue
            if engine is None:
                status = OPTIONAL_ENGINE_STATUS.get(name, "disabled")
                self.statuses[name] = status
                if status == "no_key":
                    self._warn(OPTIONAL_ENGINE_REMEDY.get(name, f"search: {name} needs a key"))
                continue
            self.engines.append(engine)
        active = {engine.name for engine in self.engines}
        for name in OPTIONAL_ENGINES:
            if name not in active:
                self.statuses.setdefault(name, "disabled")

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def enabled(self, settings: dict[str, str]) -> bool:
        return bool(self.engines)

    def _surface(self, ctx: ProviderContext) -> None:
        for warning in self.warnings:
            if warning not in ctx.warnings:
                ctx.warnings.append(warning)

    def discover(self, ctx: ProviderContext) -> list[Candidate]:
        profile = ctx.profile
        queries = build_queries(profile, _max_queries(self.settings))
        if not queries:
            for engine in self.engines:
                self.statuses[engine.name] = "disabled"
            self._warn("search: no queries from the profile; engines were not contacted")
            self._surface(ctx)
            return []
        limit = _max_results(self.settings)
        cache_days = get_int(self.settings, "search_cache_days")
        cache_days = 7 if cache_days is None else cache_days
        region = get_setting(self.settings, "search_region").strip().lower()
        own = profile.username if profile is not None else None
        profile_fetched_at = profile.fetched_at if profile is not None else None
        found: dict[str, Candidate] = {}

        try:
            for engine in self.engines:
                if engine.name in self.disabled:
                    continue
                engine_queries = (
                    queries[: engine.max_queries] if engine.max_queries else queries
                )
                produced = 0
                status: str | None = None
                for query in engine_queries:
                    cached = None
                    if ctx.store is not None and cache_days > 0:
                        cached = get_search_cache(
                            ctx.store,
                            query,
                            engine.cache_key,
                            region,
                            cache_days,
                            profile_fetched_at,
                        )
                    if cached is None:
                        try:
                            cached = engine.search(query, limit)
                        except CaptchaError:
                            status = "captcha"
                            self.disabled.add(engine.name)
                            self._warn(
                                f"search: {engine.name} answered with CAPTCHA; skipped for this run"
                            )
                            break
                        except EngineResultError as exc:
                            status = "error"
                            self._warn(f"search: {engine.name} failed: {exc}")
                            break
                        except HttpError as exc:
                            code = exc.status_code
                            status = (
                                "quota" if code == 429 else "blocked" if code == 403 else "error"
                            )
                            self._warn(f"search: {engine.name} failed ({code or 'network'})")
                            if code in (403, 429):
                                self.disabled.add(engine.name)
                                break
                            continue
                        if ctx.store is not None and cache_days > 0:
                            put_search_cache(
                                ctx.store,
                                query,
                                engine.cache_key,
                                region,
                                cached,
                                cache_days,
                                profile_fetched_at,
                            )
                    if cached:
                        produced += 1
                    for username in cached:
                        if not is_discoverable_username(username, own) or username in found:
                            continue
                        found[username] = Candidate(
                            username=username,
                            source=f"search_{engine.name}",
                            meta={"query": query, "engine": engine.name},
                        )
                self.statuses[engine.name] = status or ("ok" if produced else "empty")
        finally:
            for engine in self.engines:
                close = getattr(engine, "close", None)
                if callable(close):
                    close()
        self._surface(ctx)
        return list(found.values())


def build_provider(settings: dict[str, str], http: Any) -> WebSearchProvider:
    return WebSearchProvider(settings, http)
