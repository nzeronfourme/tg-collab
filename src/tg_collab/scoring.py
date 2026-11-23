"""Dynamic range classification, quality flags, and contact detection."""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import Iterable

from tg_collab.config import get_float, get_int
from tg_collab.models import Channel, ChannelPage, ChannelPost, RangeVerdict
from tg_collab.profile import TargetRange

CONTACT_KEYWORDS = re.compile(
    r"(?<![\w@])(админ\w*|связ\w*|сотруднич\w*|реклам\w*|по вопросам|contact|admin|collab|promo|ads|dm)(?![\w])",
    re.IGNORECASE,
)
BOT_KEYWORDS = re.compile(
    r"(?<![\w@])(предложк\w*|бот\w*|suggest\w*|bot)(?![\w])", re.IGNORECASE
)
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")


def classify(channel: Channel, target: TargetRange) -> RangeVerdict:
    """Compare a channel with the current target range, computed on read."""
    basis = target.high / target.max_mult if target.max_mult else 0
    if target.high <= 0 or basis <= 0:
        return RangeVerdict("unknown_size")
    subs_ratio: float | None = None
    views_ratio: float | None = None
    if channel.subscribers is not None:
        value = channel.subscribers
        subs_ratio = round(value / basis, 4)
    elif channel.avg_views is not None:
        value = channel.avg_views
        views_ratio = round(value / basis, 4)
    else:
        return RangeVerdict("unknown_size")
    if value < target.low:
        kind = "too_small"
    elif value > target.high:
        kind = "too_big"
    else:
        kind = "in_range"
    return RangeVerdict(kind, subs_ratio=subs_ratio, views_ratio=views_ratio)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _has_inactive(last_post_at: str | None, days: int) -> bool:
    parsed = _parse_dt(last_post_at)
    if parsed is None:
        return False
    return (datetime.now(timezone.utc) - parsed).total_seconds() > days * 86400


def _has_suspicious_views(posts: Iterable[ChannelPost], err: float | None, settings: dict[str, str]) -> bool:
    error_threshold = get_float(settings, "flag_err_threshold")
    if error_threshold is None:
        error_threshold = 0.5
    if err is not None and err > error_threshold:
        return True

    newest = list(posts)[:20]
    views = [post.views for post in newest if post.views is not None]
    if len(views) < 2:
        return False
    mean = sum(views) / len(views)
    if mean <= 0:
        return False
    cv_threshold = get_float(settings, "flag_cv_threshold")
    if cv_threshold is None:
        cv_threshold = 0.8
    ratio_threshold = get_float(settings, "flag_ratio_threshold")
    if ratio_threshold is None:
        ratio_threshold = 5.0
    coefficient = statistics.pstdev(views) / mean
    median = statistics.median(views)
    ratio = max(views) / median if median else 0.0
    return coefficient > cv_threshold or ratio > ratio_threshold


def detect_flags(
    channel: Channel,
    posts: Iterable[ChannelPost],
    settings: dict[str, str],
) -> list[str]:
    inactive_days = get_int(settings, "flag_inactive_days")
    if inactive_days is None:
        inactive_days = 14

    flags: list[str] = []
    if _has_inactive(channel.last_post_at, inactive_days):
        flags.append("inactive")
    if _has_suspicious_views(posts, channel.err, settings):
        flags.append("suspicious_views")
    if channel.meta.get("preview_disabled"):
        flags.append("preview_disabled")
    return flags


def detect_contact(page: ChannelPage) -> tuple[str, str | None, list[str]]:
    """Return ``(contact_status, contact_value, candidates)`` for a parsed page."""
    text = "\n".join(part for part in (page.description, page.pinned_text) if part)
    all_mentions: list[str] = []
    direct: list[str] = []
    bots: list[str] = []
    bot_context = False

    for line in re.split(r"[\n.!?]+", text):
        mentions = MENTION_RE.findall(line)
        if not mentions:
            continue
        stripped = MENTION_RE.sub(" ", line)
        if BOT_KEYWORDS.search(stripped):
            bot_context = True
        if CONTACT_KEYWORDS.search(stripped):
            for name in mentions:
                candidate = f"@{name}"
                if name.lower().endswith("_bot"):
                    bots.append(candidate)
                else:
                    direct.append(candidate)
        for name in mentions:
            candidate = f"@{name}"
            if candidate not in all_mentions:
                all_mentions.append(candidate)
            if name.lower().endswith("_bot") and candidate not in bots:
                bots.append(candidate)

    if direct:
        return "direct_admin", direct[0], all_mentions
    if bots or (bot_context and all_mentions):
        value = bots[0] if bots else all_mentions[0]
        return "bot_or_suggest", value, all_mentions
    if page.comments_enabled:
        return "comments_open", "comments", all_mentions
    return "manual_needed", None, all_mentions
