"""Channel profile metrics and the dynamic target range."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from tg_collab.models import ChannelPage, ChannelPost, Profile
from tg_collab.sources.tg_public import extract_links


@dataclass(frozen=True)
class TargetRange:
    low: int
    high: int
    min_mult: float = 0.3
    max_mult: float = 3.0


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def view_stats(posts: Iterable[ChannelPost]) -> tuple[int | None, int | None]:
    """Average and median over posts that expose views; nulls are ignored."""
    views = sorted(post.views for post in posts if post.views is not None)
    if not views:
        return None, None
    average = int(round(sum(views) / len(views)))
    middle = len(views) // 2
    if len(views) % 2:
        median = views[middle]
    else:
        median = int(round((views[middle - 1] + views[middle]) / 2))
    return average, median


def post_cadence(posts: Iterable[ChannelPost]) -> float:
    """Posts per week across the oldest-newest span, at least one day wide."""
    items = list(posts)
    if len(items) < 2:
        return 0.0
    dates = [date for date in (_parse_dt(post.posted_at) for post in items) if date is not None]
    if len(dates) < 2:
        return 0.0
    span_days = max((max(dates) - min(dates)).total_seconds() / 86400.0, 1.0)
    return round((len(dates) - 1) * 7.0 / span_days, 2)


def build_profile(
    page: ChannelPage,
    posts: Iterable[ChannelPost],
    tgstat: dict | None = None,
) -> Profile:
    items = list(posts)
    average, median = view_stats(items)
    subscribers = page.subscribers
    if not subscribers:
        err = 0.0
    elif average is None:
        err = None
    else:
        err = round(average / subscribers, 4)

    newest = max(items, key=lambda post: post.message_id) if items else None
    links = list(page.links)
    for link in extract_links(page.description, page.pinned_text):
        if link not in links:
            links.append(link)

    return Profile(
        username=page.username,
        title=page.title,
        description=page.description,
        subscribers=subscribers,
        avg_views=average,
        median_views=median,
        err=err,
        posts_per_week=post_cadence(items),
        links=links,
        pinned_text=page.pinned_text,
        last_post_at=newest.posted_at if newest is not None else None,
        tgstat=tgstat,
    )


def derive_range(profile: Profile, min_mult: float = 0.3, max_mult: float = 3.0) -> TargetRange:
    """Target partner size from subscribers, falling back to average views."""
    basis = profile.subscribers or profile.avg_views or 0
    return TargetRange(
        low=int(round(basis * min_mult)),
        high=int(round(basis * max_mult)),
        min_mult=min_mult,
        max_mult=max_mult,
    )
