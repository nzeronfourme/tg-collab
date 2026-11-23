"""Service functions shared by the CLI commands and the background jobs."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from tg_collab.config import get_float, get_int, get_setting, normalize_proxy, set_settings
from tg_collab.errors import TgCollabError
from tg_collab.http_client import HttpClient, HttpError
from tg_collab.models import Lead, Profile
from tg_collab.profile import TargetRange, build_profile, derive_range
from tg_collab.similarity import posts_to_json, rescore_channels
from tg_collab.sources.registry import build_enrichment_providers
from tg_collab.sources.tg_public import (
    TelegramPublicClient,
    extract_invite_links,
    parse_invite_page,
    resolve_username,
)
from tg_collab.store import backfill_channel_languages, upsert_lead, upsert_profile


def make_http(settings: dict[str, str], transport: Any = None) -> HttpClient:
    retries = get_int(settings, "http_retries")
    timeout = get_float(settings, "http_timeout_sec")
    delay = get_int(settings, "http_delay_ms")
    proxy = None if transport is not None else normalize_proxy(get_setting(settings, "proxy_url"))
    return HttpClient(
        transport=transport,
        proxy=proxy or None,
        delay_ms=delay if delay is not None else 1000,
        timeout=timeout if timeout is not None else 20,
        retries=retries if retries is not None else 2,
    )


def check_telegram(settings: dict[str, str], proxy_url: str | None = None) -> dict[str, Any]:
    """Probe t.me/s/telegram through the configured (or submitted) proxy."""
    raw_proxy = get_setting(settings, "proxy_url") if proxy_url is None else proxy_url
    proxy = normalize_proxy(raw_proxy)
    timeout = get_float(settings, "http_timeout_sec") or 20
    try:
        client = HttpClient(proxy=proxy or None, timeout=min(timeout, 20), retries=0, delay_ms=0)
    except Exception as exc:  # noqa: BLE001 - invalid proxy URL is a user error
        return {"ok": False, "error": f"invalid proxy: {exc}"}
    started = time.monotonic()
    try:
        response = client.get("https://t.me/s/telegram")
        return {
            "ok": True,
            "status": response.status_code,
            "ms": int((time.monotonic() - started) * 1000),
            "bytes": len(response.text),
        }
    except HttpError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        client.close()


def multipliers(settings: dict[str, str]) -> tuple[float, float]:
    return (
        get_float(settings, "min_mult") or 0.3,
        get_float(settings, "max_mult") or 3.0,
    )


def capture_invite_lead(
    conn: sqlite3.Connection,
    http: HttpClient,
    lead: Lead,
    profile_id: int | None = None,
) -> Lead:
    """Store an invite lead, filling title/size from the public landing page.

    The landing page is best-effort: a network error, a 4xx, or a page without
    title and size keeps the canonical URL and records why the metadata is
    missing. Nothing here logs in or joins.
    """
    title: str | None = None
    subscribers: int | None = None
    note = lead.note
    try:
        response = http.get(lead.url)
        meta = parse_invite_page(response.text)
        title, subscribers = meta["title"], meta["subscribers"]
        if title is None and subscribers is None:
            reason = "public landing metadata unavailable; verify manually"
            note = f"{note}; {reason}" if note else reason
    except Exception as exc:  # noqa: BLE001 - metadata must never fail the caller
        reason = f"public landing metadata unavailable ({type(exc).__name__}); verify manually"
        note = f"{note}; {reason}" if note else reason
    return upsert_lead(
        conn,
        Lead(url=lead.url, title=title, subscribers=subscribers, source=lead.source, note=note),
        profile_id=profile_id,
    )


def init_profile(
    conn: sqlite3.Connection,
    settings: dict[str, str],
    http: HttpClient,
    channel: str,
    posts_limit: int | None = None,
    profile_id: int | None = None,
) -> dict[str, Any]:
    """Profile the owner's channel, persist it, and derive the target range."""
    backfill_channel_languages(conn)
    try:
        username = resolve_username(channel)
    except ValueError as exc:
        raise TgCollabError(str(exc)) from None

    client = TelegramPublicClient(http)
    page = client.fetch_page(username)
    if not page.has_preview:
        raise TgCollabError(
            f"@{username} has no public link preview, so its metrics cannot be read without login. "
            "Enable the public link preview in the channel settings (Channel settings -> Public link "
            "preview) and run init again.",
        )

    limit = posts_limit if posts_limit is not None else (get_int(settings, "profile_post_limit") or 30)
    posts = client.fetch_posts(username, limit)

    warnings: list[str] = []
    tgstat: dict | None = None
    for provider in build_enrichment_providers(settings, http, warnings):
        enriched = provider.enrich(username)
        if enriched is None:
            continue
        if enriched.subscribers is not None and page.subscribers is None:
            page.subscribers = enriched.subscribers
        if enriched.tgstat:
            tgstat = enriched.tgstat

    profile = build_profile(page, posts, tgstat=tgstat)
    profile.posts_json = posts_to_json(posts, limit=limit)
    if not posts:
        warnings.append(f"@{username}: no posts for the niche profile")
    stored = upsert_profile(conn, profile, profile_id=profile_id)
    set_settings(conn, {"mine_username": stored.username})
    # Invite-only channels appear in the owner's own text only; third-party
    # candidate posts are never mined here.
    invite_origins: dict[str, list[str]] = {}
    own_text = [(page.description, "description"), (page.pinned_text, "pinned post")]
    own_text.extend((post.text, "recent posts") for post in posts)
    for text, origin in own_text:
        for invite_url in extract_invite_links(text):
            origins = invite_origins.setdefault(invite_url, [])
            if origin not in origins:
                origins.append(origin)
    for invite_url, origins in invite_origins.items():
        note = f"Found in the {' and '.join(origins)} of @{stored.username}"
        capture_invite_lead(
            conn, http, Lead(url=invite_url, source="init", note=note), profile_id=stored.id
        )
    rescore_channels(conn, stored, settings, profile_id=stored.id)
    min_mult, max_mult = multipliers(settings)
    target = derive_range(stored, min_mult, max_mult)
    return {
        "profile": stored,
        "range": target,
        "fetched_posts": len(posts),
        "warnings": warnings,
    }


def profile_json(profile: Profile) -> dict[str, Any]:
    return {
        "username": profile.username,
        "title": profile.title,
        "description": profile.description,
        "subscribers": profile.subscribers,
        "avg_views": profile.avg_views,
        "median_views": profile.median_views,
        "err": profile.err,
        "posts_per_week": profile.posts_per_week,
        "links": profile.links,
        "pinned_text": profile.pinned_text,
        "last_post_at": profile.last_post_at,
        "tgstat": profile.tgstat,
        "fetched_at": profile.fetched_at,
    }


def range_json(target: TargetRange) -> dict[str, Any]:
    return {
        "low": target.low,
        "high": target.high,
        "min_mult": target.min_mult,
        "max_mult": target.max_mult,
    }
