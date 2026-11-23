"""Telegram Public (``t.me/s/<username>``) parsing, mentions, and pagination."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from tg_collab.http_client import HttpError
from tg_collab.models import ChannelPage, ChannelPost

logger = logging.getLogger(__name__)

MESSAGE_SELECTOR = ".tgme_widget_message[data-post]"
INFO_SELECTOR = ".tgme_channel_info"
MAX_PAGES = 20

RESERVED_USERNAMES = frozenset(
    {
        "joinchat",
        "s",
        "proxy",
        "socks",
        "addstickers",
        "setlanguage",
        "share",
        "iv",
        "telegram",
        "botfather",
        "userinfobot",
    }
)

_NUMBER_RE = re.compile(r"(\d[\d\s.,]*)\s*(тыс\.?|млн\.?|млрд\.?|k|m|b)?", re.IGNORECASE)
_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "тыс": 1_000,
    "млн": 1_000_000,
    "млрд": 1_000_000_000,
}
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|(?<![\w@/])t\.me/[^\s<>\"']+", re.IGNORECASE)


def parse_count(text: str | None) -> int | None:
    """Parse ``1 234``, ``1,234``, ``1.2K``, ``1,2 тыс``, ``1,2 млн`` into an int."""
    if not text:
        return None
    cleaned = text.replace("\u00a0", " ").replace("\u202f", " ").strip()
    match = _NUMBER_RE.search(cleaned)
    if match is None:
        return None
    number = match.group(1).strip()
    suffix = (match.group(2) or "").lower().rstrip(".")
    if suffix:
        numeric = number.replace(" ", "").replace(",", ".")
        parts = numeric.split(".")
        numeric = "".join(parts[:-1]) + "." + parts[-1] if len(parts) > 1 else numeric
        try:
            value = float(numeric)
        except ValueError:
            return None
        return int(round(value * _MULTIPLIERS.get(suffix, 1)))
    digits = re.sub(r"\D", "", number)
    return int(digits) if digits else None


def parse_post(node: Tag) -> ChannelPost | None:
    data_post = node.get("data-post")
    if not data_post or "/" not in str(data_post):
        return None
    raw_id = str(data_post).rsplit("/", 1)[-1]
    if not raw_id.isdigit():
        return None
    text_el = node.select_one(".tgme_widget_message_text")
    views_el = node.select_one(".tgme_widget_message_views")
    time_el = node.select_one(".tgme_widget_message_date time[datetime]")
    return ChannelPost(
        message_id=int(raw_id),
        posted_at=str(time_el.get("datetime")) if time_el is not None else "",
        views=parse_count(views_el.get_text(strip=True)) if views_el is not None else None,
        text=text_el.get_text(" ", strip=True) if text_el is not None else None,
        forwarded_from=_forwarded_username(node),
    )


def _forwarded_username(node: Tag) -> str | None:
    name = node.select_one(".tgme_widget_message_forwarded_from_name")
    if name is None:
        return None
    anchor = name.find_parent("a") or node.select_one(".tgme_widget_message_forwarded_from a")
    href = anchor.get("href") if isinstance(anchor, Tag) else None
    if not href:
        return None
    return _username_from_href(str(href))


def _username_from_href(href: str) -> str | None:
    path = urlsplit(href).path.strip("/")
    if not path:
        return None
    if path.startswith("s/"):
        path = path[2:]
    candidate = path.split("/", 1)[0].lstrip("@")
    return candidate or None


def has_comments(soup: BeautifulSoup) -> bool:
    for anchor in soup.select("a[href]"):
        if "comment=" in str(anchor.get("href")):
            return True
    return soup.select_one(".tgme_widget_message_replies, .tgme_widget_message_replies_wrap") is not None


def _pinned_text(soup: BeautifulSoup) -> str | None:
    for element in soup.select("[class*=pinned]"):
        text_el = element.select_one(".tgme_widget_message_text")
        if text_el is None:
            for parent in element.parents:
                if isinstance(parent, Tag) and "tgme_widget_message" in (parent.get("class") or []):
                    text_el = parent.select_one(".tgme_widget_message_text")
                    break
        if text_el is not None:
            return text_el.get_text(" ", strip=True)
    return None


def _collect_links(soup: BeautifulSoup) -> list[str]:
    links: list[str] = []
    scope = soup.select(".tgme_channel_info_description, [class*=pinned]")
    for block in scope:
        for anchor in block.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            if href.startswith("http://") or href.startswith("https://"):
                if href not in links:
                    links.append(href)
    return links


def parse_channel_page(html: str, expected_username: str | None = None) -> ChannelPage:
    """Parse a public preview page. Pure HTML in, records out; no network."""
    soup = BeautifulSoup(html, "lxml")
    info = soup.select_one(INFO_SELECTOR)
    posts = [post for post in (parse_post(node) for node in soup.select(MESSAGE_SELECTOR)) if post]

    username = expected_username or ""
    if not username:
        username_anchor = soup.select_one(".tgme_channel_info_header_username a[href]")
        href = str(username_anchor.get("href")) if username_anchor is not None else ""
        username = _username_from_href(href) or "" if href else ""

    title_el = soup.select_one(".tgme_channel_info_header_title")
    description_el = soup.select_one(".tgme_channel_info_description")

    subscribers: int | None = None
    for counter in soup.select(".tgme_channel_info_counter"):
        counter_type = counter.select_one(".counter_type")
        type_text = counter_type.get_text(" ", strip=True).lower() if counter_type is not None else ""
        if "subscriber" in type_text or "подписчик" in type_text:
            value_el = counter.select_one(".counter_value")
            subscribers = parse_count(value_el.get_text(strip=True) if value_el is not None else None)
            break

    return ChannelPage(
        username=username,
        title=title_el.get_text(" ", strip=True) if title_el is not None else None,
        description=description_el.get_text(" ", strip=True) if description_el is not None else None,
        subscribers=subscribers,
        posts=posts,
        has_preview=info is not None or bool(posts),
        comments_enabled=has_comments(soup),
        pinned_text=_pinned_text(soup),
        links=_collect_links(soup),
    )


def is_preview_disabled(html: str, final_url: str | None) -> bool:
    """A redirect outside ``/s/`` or a page without channel info and messages."""
    if final_url:
        if "/s/" not in urlsplit(final_url).path:
            return True
    soup = BeautifulSoup(html, "lxml")
    return soup.select_one(INFO_SELECTOR) is None and soup.select_one(MESSAGE_SELECTOR) is None


def extract_links(*texts: str | None) -> list[str]:
    """Unique http(s):// and t.me/ links found in plain text, in first-seen order."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _URL_RE.findall(text):
            link = match.rstrip(").,;:!?'\"]}")
            if link.startswith("t.me/"):
                link = "https://" + link
            if link not in found:
                found.append(link)
    return found


INVITE_HASH_RE = re.compile(r"[A-Za-z0-9_-]{4,128}")
_INVITE_URL_RE = re.compile(
    r"(?<![\w/])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/\+([A-Za-z0-9_-]{4,128})",
    re.IGNORECASE,
)
# The landing extra is "1 234 members, 12 online"; a lone "m" must not read as millions.
_INVITE_COUNT_RE = re.compile(
    r"\s*(\d[\d\s.,]*)\s*(?:(тыс\.?|млн\.?|млрд\.?)|([kmb])\b)?",
    re.IGNORECASE,
)


def extract_invite_links(*texts: str | None) -> list[str]:
    """Canonical ``https://t.me/+<hash>`` invite links in first-seen order, deduped.

    Invite hashes are case-sensitive and only linked from the owner's own text
    or an explicit import; they never become usernames or graph edges.
    """
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _INVITE_URL_RE.finditer(text):
            url = f"https://t.me/+{match.group(1)}"
            if url not in found:
                found.append(url)
    return found


def parse_invite_page(html: str) -> dict[str, Any]:
    """Best-effort title and size from a public invite landing page; no login."""
    soup = BeautifulSoup(html, "lxml")
    title: str | None = None
    title_el = soup.select_one(".tgme_page_title")
    if title_el is not None:
        text = title_el.get_text(" ", strip=True)
        if text and not text.lower().startswith("telegram:"):
            title = text
    if title is None:
        og = soup.select_one('meta[property="og:title"]')
        content = og.get("content") if og is not None else None
        if content and not str(content).lower().startswith("telegram:"):
            title = str(content)
    subscribers: int | None = None
    extra = soup.select_one(".tgme_page_extra")
    if extra is not None:
        count = _INVITE_COUNT_RE.match(extra.get_text(" ", strip=True))
        subscribers = parse_count(count.group(0)) if count is not None else None
    return {"title": title, "subscribers": subscribers}


_USERNAME_MENTION_RE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{4,32})")
_USERNAME_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,32})",
    re.IGNORECASE,
)


def extract_usernames(text: str | None) -> set[str]:
    """``@name``, ``t.me/name``, ``https://t.me/name/123`` and ``telegram.me/name``."""
    if not text:
        return set()
    found = {match.group(1).lower() for match in _USERNAME_MENTION_RE.finditer(text)}
    found.update(match.group(1).lower() for match in _USERNAME_URL_RE.finditer(text))
    return found


def extract_telegram_usernames(text: str | None) -> set[str]:
    """Only explicit ``t.me``/``telegram.me`` links, safe for READMEs and bios."""
    if not text:
        return set()
    return {match.group(1).lower() for match in _USERNAME_URL_RE.finditer(text)}


def is_discoverable_username(name: str, own: str | None = None) -> bool:
    """Reject reserved paths, bots, the owner, and malformed usernames."""
    lowered = (name or "").lower()
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", lowered):
        return False
    if lowered in RESERVED_USERNAMES or lowered.endswith("_bot"):
        return False
    return lowered != (own or "").lower()


def ordered_telegram_usernames(text: str | None) -> list[str]:
    """Explicit ``t.me`` links in first-seen document order, deduped, junk dropped.

    Search engines return results in relevance order, so callers that truncate
    must keep that order instead of sorting. Reserved paths and bots are
    removed before the caller's limit so they never consume a result slot.
    """
    if not text:
        return []
    names = (
        match.group(1).lower()
        for match in _USERNAME_URL_RE.finditer(text)
        if is_discoverable_username(match.group(1))
    )
    return list(dict.fromkeys(names))


def _is_discoverable(name: str, own: str | None) -> bool:
    return is_discoverable_username(name, own)


def extract_page_edges(page: ChannelPage) -> set[str]:
    """Channel usernames reachable from a parsed page: mentions, reposts, URLs."""
    texts = [page.description, page.pinned_text]
    texts.extend(post.text for post in page.posts)
    found: set[str] = set()
    for text in texts:
        found.update(extract_usernames(text))
    found.update(extract_usernames(" ".join(page.links)))
    for post in page.posts:
        if post.forwarded_from:
            found.add(post.forwarded_from.lower())
    return {name for name in found if _is_discoverable(name, page.username)}


def resolve_username(raw: str) -> str:
    """Accept ``@name``, ``t.me/name``, ``https://t.me/name``, ``t.me/s/name``."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("empty channel username")
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:www\.)?(?:t\.me|telegram\.me)/", "", value, flags=re.IGNORECASE)
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    if value.lower().startswith("s/"):
        value = value[2:]
    value = value.lstrip("@")
    if "/" in value:
        value = value.split("/", 1)[0]
    if not value:
        raise ValueError("empty channel username")
    if value.lower() in RESERVED_USERNAMES:
        raise ValueError(f"reserved Telegram path: {value}")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", value):
        raise ValueError(f"not a valid channel username: {value}")
    return value


class TelegramPublicClient:
    """Fetch and paginate public channel previews through the shared HttpClient."""

    def __init__(self, http: Any) -> None:
        self.http = http

    def fetch_page(self, username: str, before: int | None = None) -> ChannelPage:
        username = resolve_username(username)
        url = f"https://t.me/s/{username}"
        params: dict[str, Any] | None = {"before": before} if before is not None else None
        response = self.http.get(url, params=params)
        html = response.text
        page = parse_channel_page(html, expected_username=username)
        if is_preview_disabled(html, str(response.url)):
            page.has_preview = False
        return page

    def channel_exists(self, username: str) -> bool:
        """Check the t.me landing page for channel identity markers."""
        username = resolve_username(username)
        try:
            response = self.http.get(f"https://t.me/{username}")
        except HttpError as exc:
            if exc.status_code == 404:
                return False
            raise
        soup = BeautifulSoup(response.text, "lxml")
        extra = soup.select_one(".tgme_page_extra")
        extra_text = extra.get_text(" ", strip=True).lower() if extra is not None else ""
        markers = ("subscriber", "member", "online", "подписчик", "участник", "онлайн")
        if re.search(r"\d", extra_text) and any(word in extra_text for word in markers):
            return True
        title = soup.select_one(".tgme_page_title")
        if title is None:
            return False
        text = title.get_text(" ", strip=True)
        return bool(text) and "telegram: contact" not in text.lower()

    def fetch_posts(self, username: str, limit: int) -> list[ChannelPost]:
        """Walk ``?before=<oldest id>`` until ``limit`` posts or no new ids."""
        if limit <= 0:
            return []
        collected: dict[int, ChannelPost] = {}
        requested: set[int] = set()
        before: int | None = None
        pages = 0
        while len(collected) < limit and pages < MAX_PAGES:
            if before is not None:
                if before in requested:
                    break
                requested.add(before)
            page = self.fetch_page(username, before=before)
            pages += 1
            new_posts = [post for post in page.posts if post.message_id not in collected]
            for post in page.posts:
                collected.setdefault(post.message_id, post)
            if not page.posts or not new_posts:
                break
            before = min(post.message_id for post in page.posts)
        if pages >= MAX_PAGES and len(collected) < limit:
            logger.warning("stopped paginating @%s after %s pages", username, MAX_PAGES)
        ordered = sorted(collected.values(), key=lambda post: post.message_id, reverse=True)
        return ordered[:limit]
