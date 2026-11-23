"""GitHub reference discovery plugin: repositories that link to Telegram channels."""

from __future__ import annotations

import json
import re
from typing import Any

from tg_collab.config import get_bool, get_int, get_setting
from tg_collab.http_client import HttpError
from tg_collab.models import Candidate
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.plugins.base import BaseDiscoveryPlugin
from tg_collab.sources.tg_public import extract_telegram_usernames, is_discoverable_username

API = "https://api.github.com"
TOPIC_QUERY = "topic:telegram"
AWESOME_QUERY = "awesome telegram"
MAX_PROFILE_REPOS = 5
MAX_SEARCH_QUERIES = 6

# Strict ``owner/repo`` matcher: not preceded by a word, ``/``, a scheme, or a dot,
# so ``example.com/a/b``, ``t.me/x`` and URLs inside other hosts never qualify.
_REPO_REF_RE = re.compile(
    r"(?<![\w/.:@-])"
    r"(?:(?:https?://)?(?:www\.)?github\.com/)?"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})"
    r"(?![\w/])",
    re.IGNORECASE,
)

# GitHub paths that look like ``owner/repo`` but are product pages, not repositories.
_RESERVED_OWNERS = frozenset(
    {
        "about", "account", "apps", "blog", "collections", "contact", "dashboard",
        "enterprise", "events", "explore", "features", "issues", "join", "login",
        "logout", "marketplace", "new", "notifications", "orgs", "pricing", "pulls",
        "search", "security", "settings", "signup", "site", "sponsors", "stars",
        "team", "teams", "topics", "trending", "watching",
    }
)
_PLACEHOLDER_OWNERS = frozenset(
    {
        "owner", "user", "username", "your-username", "your_username", "example",
        "org", "organization", "repo", "repository", "name",
    }
)
_PLACEHOLDER_REPOS = frozenset(
    {"repo", "repository", "your-repo", "your_repo", "example", "name", "project", "my-repo", "my_repo"}
)
# Function words and pronouns that read as ``owner/repo`` only by accident (``and/or``).
_NON_REPO_OWNERS = frozenset(
    {
        "and", "or", "the", "a", "an", "to", "of", "in", "on", "at", "by", "for",
        "with", "from", "as", "if", "is", "it", "this", "that", "his", "her",
        "its", "our", "your", "their",
    }
)

STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "your", "you", "about",
        "our", "their", "there", "here", "what", "when", "where", "which", "will",
        "have", "has", "are", "was", "were", "not", "but", "all", "any", "can",
        "канал", "канала", "канале", "каналы", "про", "для", "что", "как", "это",
        "или", "так", "все", "его", "она", "они", "мы", "вы", "мне", "тебя", "если",
    }
)


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;\n]", value) if part.strip()]


def derive_queries(profile: Any) -> list[str]:
    """Hashtags plus up to five meaningful words from the owner's title/description."""
    if profile is None:
        return []
    text = " ".join(part for part in (profile.title, profile.description) if part)
    hashtags = re.findall(r"#([A-Za-zА-Яа-яЁё0-9_]{2,})", text)
    words = [
        word
        for word in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", text)
        if word.lower() not in STOPWORDS
    ]
    queries: list[str] = []
    seen: set[str] = set()
    for token in [*hashtags, *words]:
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        queries.append(token)
        if len(queries) >= 5:
            break
    return queries


def derive_search_queries(profile: Any) -> list[str]:
    """``topic:telegram``, the awesome list, plus README-scoped niche terms."""
    queries: list[str] = []
    seen: set[str] = set()
    for query in (
        TOPIC_QUERY,
        AWESOME_QUERY,
        *(f"{term} in:readme" for term in derive_queries(profile)),
    ):
        if query.lower() in seen:
            continue
        seen.add(query.lower())
        queries.append(query)
        if len(queries) >= MAX_SEARCH_QUERIES:
            break
    return queries


def _is_repo_reference(owner: str, repo: str) -> bool:
    owner_lower = owner.lower()
    repo_lower = repo.lower()
    if owner_lower in _RESERVED_OWNERS or owner_lower in _PLACEHOLDER_OWNERS:
        return False
    if owner_lower in _NON_REPO_OWNERS:
        return False
    if repo_lower in _PLACEHOLDER_REPOS:
        return False
    if repo.isdigit():
        return False
    return not (owner_lower == "t" and repo_lower == "me")


def profile_repo_refs(profile: Any) -> list[str]:
    """Up to five strict ``owner/repo`` references found in the owner's posts."""
    if profile is None:
        return []
    try:
        posts = json.loads(profile.posts_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(posts, list):
        return []
    text = " ".join(
        str(post.get("text") or "") for post in posts if isinstance(post, dict)
    )
    refs: list[str] = []
    seen: set[str] = set()
    for match in _REPO_REF_RE.finditer(text):
        owner, repo = match.group(1), match.group(2).rstrip(".")
        if not _is_repo_reference(owner, repo):
            continue
        full_name = f"{owner}/{repo}"
        key = full_name.lower()
        if key in seen:
            continue
        seen.add(key)
        refs.append(full_name)
        if len(refs) >= MAX_PROFILE_REPOS:
            break
    return refs


class GithubPlugin(BaseDiscoveryPlugin):
    name = "plugin_github"

    def enabled(self, settings: dict[str, str]) -> bool:
        return get_bool(settings, "sources_plugin_github")

    def _warn(self, ctx: ProviderContext | None, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        if ctx is not None and message not in ctx.warnings:
            ctx.warnings.append(message)

    def _finished(
        self,
        ctx: ProviderContext | None,
        candidates: list[Candidate],
        *,
        searches_ok: int,
        blocked: bool,
    ) -> list[Candidate]:
        """Report a truthful status: a failed search is not an empty answer."""
        if candidates:
            status = "ok"
        elif blocked:
            status = "blocked"
        elif searches_ok:
            status = "empty"
        else:
            status = "error"
        if ctx is not None:
            ctx.statuses[self.name] = status
        return candidates

    def discover(self, ctx: ProviderContext) -> list[Candidate]:
        queries = _split_list(get_setting(self.settings, "github_queries"))
        if not queries:
            queries = derive_search_queries(ctx.profile)
        if not queries:
            self._warn(ctx, "github: no queries - set github_queries in the settings page")
            ctx.statuses[self.name] = "disabled"
            return []

        token = get_setting(self.settings, "github_token").strip()
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        max_repos = get_int(self.settings, "github_max_repos") or 20
        if not token:
            unauth = get_int(self.settings, "github_max_repos_unauth") or 10
            max_repos = min(max_repos, unauth)
        max_repos = min(max_repos, 20)

        # Collect every repository first: profile references, then all search hits.
        collected: list[tuple[str, dict[str, Any] | None]] = []
        seen: set[str] = set()
        for full_name in profile_repo_refs(ctx.profile):
            seen.add(full_name.lower())
            collected.append((full_name, None))
        searches_ok = 0
        blocked = False
        for query in queries:
            try:
                payload = self.http.get_json(
                    f"{API}/search/repositories",
                    params={"q": query, "per_page": 30},
                    headers=headers,
                )
            except HttpError as exc:
                if exc.status_code == 403:
                    blocked = True
                    self._warn(ctx, "github: rate limit or forbidden (403); stopping")
                    break
                detail = "network error" if exc.status_code == 0 else str(exc.status_code)
                self._warn(ctx, f"github: search failed ({detail})")
                continue
            searches_ok += 1
            for item in payload.get("items") or []:
                full_name = str(item.get("full_name") or "").strip()
                if "/" not in full_name or full_name.lower() in seen:
                    continue
                seen.add(full_name.lower())
                collected.append((full_name, item))

        # Only now spend the shared repository budget, profile references first.
        candidates: dict[str, Candidate] = {}
        visited = 0
        for full_name, item in collected:
            if visited >= max_repos:
                break
            visited += 1
            texts: list[str] = []
            if item is None:
                try:
                    repo = self.http.get_json(f"{API}/repos/{full_name}", headers=headers)
                except HttpError as exc:
                    if exc.status_code == 403:
                        self._warn(ctx, "github: rate limit reached while reading a repository; stopping")
                        return self._finished(
                            ctx,
                            list(candidates.values()),
                            searches_ok=searches_ok,
                            blocked=True,
                        )
                    self._warn(ctx, f"github: repository lookup failed for {full_name} ({exc.status_code})")
                    continue
                owner = str((repo.get("owner") or {}).get("login") or full_name.split("/", 1)[0])
                title = str(repo.get("full_name") or full_name)
                description = repo.get("description")
                stars = repo.get("stargazers_count")
                texts.extend(str(repo.get(key) or "") for key in ("homepage", "description"))
            else:
                owner = str((item.get("owner") or {}).get("login") or "")
                title = full_name
                description = item.get("description")
                stars = item.get("stargazers_count")
                texts.extend(str(item.get(key) or "") for key in ("homepage", "description"))
            try:
                texts.append(
                    self.http.get_text(
                        f"{API}/repos/{full_name}/readme",
                        headers={**headers, "Accept": "application/vnd.github.raw"},
                    )
                )
            except HttpError as exc:
                if exc.status_code == 403:
                    self._warn(ctx, "github: rate limit reached while reading a README; stopping")
                    return self._finished(
                        ctx,
                        list(candidates.values()),
                        searches_ok=searches_ok,
                        blocked=True,
                    )
                self._warn(ctx, f"github: README failed for {full_name} ({exc.status_code})")
            if owner:
                try:
                    user = self.http.get_json(f"{API}/users/{owner}", headers=headers)
                except HttpError:
                    user = {}
                texts.extend(str(user.get(key) or "") for key in ("bio", "blog", "html_url"))
            usernames = {
                username
                for username in extract_telegram_usernames(" ".join(texts))
                if is_discoverable_username(username)
            }
            for username in usernames:
                if username in candidates:
                    continue
                candidates[username] = Candidate(
                    username=username,
                    source=self.name,
                    title=title or None,
                    description=description,
                    meta={
                        "repo": full_name,
                        "stars": stars,
                        "description": description,
                    },
                )
        return self._finished(
            ctx,
            list(candidates.values()),
            searches_ok=searches_ok,
            blocked=blocked,
        )
