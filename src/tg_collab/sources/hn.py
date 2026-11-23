"""Keyless Hacker News discovery through the public Algolia search API."""

from __future__ import annotations

from typing import Any

from tg_collab.config import get_bool
from tg_collab.http_client import HttpError
from tg_collab.models import Candidate
from tg_collab.similarity import top_terms
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.tg_public import extract_telegram_usernames, is_discoverable_username

API_URL = "https://hn.algolia.com/api/v1/search"
QUERY_TERMS = 3
HITS_PER_PAGE = 50
TEXT_FIELDS = ("url", "title", "story_text", "comment_text")


class HnProvider:
    """Search HN stories and comments for ``t.me`` links; no key, no login."""

    name = "search_hn"

    def __init__(self, settings: dict[str, str], http: Any) -> None:
        self.settings = settings
        self.http = http
        self.status = "idle"
        self.warnings: list[str] = []

    def enabled(self, settings: dict[str, str]) -> bool:
        return get_bool(settings, "sources_search_hn")

    def _warn(self, ctx: ProviderContext | None, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        if ctx is not None and message not in ctx.warnings:
            ctx.warnings.append(message)

    def _publish(self, ctx: ProviderContext | None, status: str) -> None:
        self.status = status
        if ctx is not None:
            ctx.statuses[self.name] = status

    def discover(self, ctx: ProviderContext | None) -> list[Candidate]:
        if not self.enabled(self.settings):
            self._publish(ctx, "disabled")
            return []
        profile = ctx.profile if ctx is not None else None
        terms = top_terms(profile, QUERY_TERMS)
        if not terms:
            self._warn(ctx, "search_hn: no niche terms from the profile; nothing to query")
            self._publish(ctx, "disabled")
            return []

        found: dict[str, Candidate] = {}
        answers = 0
        failure: str | None = None
        for term in terms:
            try:
                payload = self.http.get_json(
                    API_URL,
                    params={
                        "query": term,
                        "tags": "(story,comment)",
                        "hitsPerPage": HITS_PER_PAGE,
                    },
                )
            except HttpError as exc:
                status = "quota" if exc.status_code == 429 else "error"
                failure = failure or status
                detail = "quota" if exc.status_code == 429 else str(exc.status_code or "network")
                self._warn(ctx, f"search_hn: query {term!r} failed ({detail})")
                continue
            hits = payload.get("hits") if isinstance(payload, dict) else None
            if not isinstance(hits, list):
                failure = failure or "error"
                self._warn(ctx, f"search_hn: query {term!r} returned no hits list")
                continue
            answers += 1
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                text = " ".join(str(hit.get(field) or "") for field in TEXT_FIELDS)
                for username in extract_telegram_usernames(text):
                    if not is_discoverable_username(username) or username in found:
                        continue
                    found[username] = Candidate(
                        username=username,
                        source=self.name,
                        meta={"query": term, "objectID": hit.get("objectID")},
                    )

        if found:
            self._publish(ctx, "ok")
        elif answers:
            self._publish(ctx, "empty")
        else:
            self._publish(ctx, failure or "error")
        return list(found.values())


def build_provider(settings: dict[str, str], http: Any) -> HnProvider:
    return HnProvider(settings, http)
