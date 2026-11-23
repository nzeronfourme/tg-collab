"""Turn a candidate into a stored, scored channel."""

from __future__ import annotations

from tg_collab.config import get_int
from tg_collab.errors import ChannelNotFound
from tg_collab.language import detect_language
from tg_collab.models import Channel, Candidate, utcnow
from tg_collab.profile import post_cadence, view_stats
from tg_collab.scoring import detect_contact, detect_flags
from tg_collab.similarity import build_niche_profile, score_channel
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.registry import build_enrichment_providers
from tg_collab.sources.tg_public import TelegramPublicClient, extract_page_edges
from tg_collab.store import get_channel, get_posts, replace_posts, upsert_channel


def enrich_channel(candidate: Candidate, ctx: ProviderContext) -> Channel:
    """Fetch, score, merge metadata, persist, and return the stored channel."""
    conn = ctx.store
    settings = ctx.settings
    client = TelegramPublicClient(ctx.http)
    username = candidate.username
    existing = get_channel(conn, username=username, profile_id=ctx.profile_id)

    page = client.fetch_page(username)
    if not page.has_preview and not client.channel_exists(username):
        raise ChannelNotFound(f"@{username}: not found on t.me")
    limit = get_int(settings, "profile_post_limit") or 30
    posts = client.fetch_posts(username, limit) if page.has_preview else []
    stored_posts = []
    if not page.has_preview and existing is not None and existing.id is not None:
        stored_posts = get_posts(conn, existing.id, limit=limit)

    meta: dict = dict(existing.meta) if existing is not None else {}
    sources = dict(meta.get("sources") or {})
    previous = sources.get(candidate.source)
    if candidate.source == "agent_import":
        entry = list(previous) if isinstance(previous, list) else ([previous] if previous else [])
        if candidate.meta and all(item != candidate.meta for item in entry):
            entry.append(dict(candidate.meta))
    elif isinstance(previous, list):
        entry = list(previous)
        if candidate.meta and candidate.meta not in entry:
            entry.append(candidate.meta)
    else:
        entry = dict(previous or {})
        entry.update(candidate.meta or {})
    sources[candidate.source] = entry
    meta["sources"] = sources
    if candidate.source.startswith("plugin") and candidate.meta:
        plugin = dict(meta.get("plugin") or {})
        plugin.update(candidate.meta)
        meta["plugin"] = plugin

    if page.has_preview:
        meta.pop("preview_disabled", None)
        meta["edges"] = sorted(extract_page_edges(page))
    else:
        meta["preview_disabled"] = True
        meta["edges"] = list((existing.meta.get("edges") if existing is not None else None) or [])

    if ctx.enrichers is None:
        ctx.enrichers = build_enrichment_providers(settings, ctx.http, ctx.warnings)
    tgstat: dict | None = None
    for provider in ctx.enrichers:
        enriched = provider.enrich(username)
        for warning in getattr(provider, "warnings", []) or []:
            if warning not in ctx.warnings:
                ctx.warnings.append(warning)
        if enriched is None:
            continue
        if enriched.subscribers is not None and page.subscribers is None:
            page.subscribers = enriched.subscribers
        if enriched.tgstat:
            tgstat = enriched.tgstat
    if tgstat:
        meta["tgstat"] = tgstat

    if page.has_preview:
        average, median = view_stats(posts)
        subscribers = page.subscribers
        if not subscribers:
            err = 0.0
        elif average is None:
            err = None
        else:
            err = round(average / subscribers, 4)
        newest = max(posts, key=lambda post: post.message_id) if posts else None
        last_post_at = newest.posted_at if newest is not None else None
        cadence = post_cadence(posts)
        contact_status, contact_value, contact_candidates = detect_contact(page)
        meta["contact_candidates"] = contact_candidates
    else:
        subscribers = page.subscribers
        if existing is not None:
            subscribers = subscribers or existing.subscribers
            average = existing.avg_views
            median = existing.median_views
            err = existing.err
            cadence = existing.posts_per_week
            last_post_at = existing.last_post_at
            contact_status = existing.contact_status
            contact_value = existing.contact_value
            meta["contact_candidates"] = list(existing.meta.get("contact_candidates") or [])
        else:
            average = median = err = cadence = last_post_at = None
            contact_status, contact_value = "manual_needed", None
            meta["contact_candidates"] = []
        ctx.warnings.append(f"@{username}: public preview is disabled, metrics are unavailable")

    title = page.title or candidate.title or (existing.title if existing is not None else None)
    description = page.description or candidate.description or (
        existing.description if existing is not None else None
    )
    niche = build_niche_profile(ctx.profile)
    similarity, size_fit, score = score_channel(
        niche,
        ctx.profile,
        settings,
        subscribers,
        title,
        description,
        posts or stored_posts,
    )
    detected = detect_language(
        title, description, *(post.text for post in (posts or stored_posts))
    )
    # A re-enrichment without readable text must not blank a known language.
    lang = detected
    if detected == "unknown" and existing is not None and existing.lang:
        lang = existing.lang
    channel = Channel(
        username=username,
        source=candidate.source,
        title=title,
        description=description,
        lang=lang,
        subscribers=subscribers,
        avg_views=average,
        median_views=median,
        err=err,
        posts_per_week=cadence,
        last_post_at=last_post_at,
        contact_status=contact_status,
        contact_value=contact_value,
        similarity=similarity,
        size_fit=size_fit,
        score=score,
        meta=meta,
        checked_at=utcnow(),
    )
    channel.flags = detect_flags(channel, posts, settings)
    stored = upsert_channel(conn, channel, profile_id=ctx.profile_id)
    assert stored.id is not None
    if page.has_preview:
        replace_posts(conn, stored.id, posts, keep=limit)
    return stored
