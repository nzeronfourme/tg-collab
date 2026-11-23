"""Typed records shared by the CLI, the web UI, and providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STATUSES = ("found", "shortlisted", "drafted", "sent", "replied", "agreed", "done", "ignored")
CONTACT_STATUSES = ("direct_admin", "bot_or_suggest", "comments_open", "manual_needed")
RANGE_KINDS = ("in_range", "too_big", "too_small", "unknown_size")
JOB_STATUSES = ("running", "done", "error", "cancelled")


def utcnow() -> str:
    """ISO-8601 UTC timestamp with second precision, used everywhere in the DB."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Profile:
    username: str
    title: str | None = None
    description: str | None = None
    subscribers: int | None = None
    avg_views: int | None = None
    median_views: int | None = None
    err: float | None = None
    posts_per_week: float | None = None
    links: list[str] = field(default_factory=list)
    pinned_text: str | None = None
    last_post_at: str | None = None
    tgstat: dict[str, Any] | None = None
    posts_json: str = "[]"
    niche_json: str = ""
    seed_channels: str = ""
    fetched_at: str = ""
    id: int | None = None


@dataclass
class Lead:
    """An invite-only link that cannot be resolved without joining."""

    url: str
    id: int | None = None
    title: str | None = None
    description: str | None = None
    subscribers: int | None = None
    source: str | None = None
    note: str | None = None
    created_at: str = ""
    profile_id: int | None = None


@dataclass
class ChannelPost:
    message_id: int
    posted_at: str
    views: int | None = None
    text: str | None = None
    forwarded_from: str | None = None


@dataclass
class Channel:
    username: str
    source: str
    id: int | None = None
    title: str | None = None
    description: str | None = None
    lang: str = ""
    subscribers: int | None = None
    avg_views: int | None = None
    median_views: int | None = None
    err: float | None = None
    posts_per_week: float | None = None
    last_post_at: str | None = None
    contact_status: str = "manual_needed"
    contact_value: str | None = None
    status: str = "found"
    similarity: int | None = None
    size_fit: float | None = None
    score: float | None = None
    flags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    discovered_at: str = ""
    updated_at: str = ""
    checked_at: str | None = None
    profile_id: int | None = None


@dataclass
class Draft:
    channel_id: int
    text: str
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"
    progress: int = 0
    message: str = ""
    log: str = ""
    result: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None
    profile_id: int | None = None


@dataclass
class RangeVerdict:
    kind: str
    subs_ratio: float | None = None
    views_ratio: float | None = None


@dataclass
class Candidate:
    username: str
    source: str
    title: str | None = None
    description: str | None = None
    subscribers: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelPage:
    """Parsed public preview page of a channel (see sources/tg_public.py)."""

    username: str
    title: str | None = None
    description: str | None = None
    subscribers: int | None = None
    posts: list[ChannelPost] = field(default_factory=list)
    has_preview: bool = True
    comments_enabled: bool = False
    pinned_text: str | None = None
    links: list[str] = field(default_factory=list)
    tgstat: dict[str, Any] | None = None
