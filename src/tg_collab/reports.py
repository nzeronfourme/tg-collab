"""Row building and export writers shared by the CLI and the web UI."""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any

from tg_collab.config import get_float
from tg_collab.language import CJK, CYRILLIC, LATIN, UNKNOWN
from tg_collab.models import Channel, Profile, RangeVerdict
from tg_collab.profile import TargetRange, derive_range
from tg_collab.scoring import classify, detect_flags
from tg_collab.similarity import (
    NicheProfile,
    build_niche_profile,
    owner_language,
    tokenize,
)
from tg_collab.store import get_posts, latest_draft, list_channels

STATUS_ORDER = ("found", "shortlisted", "drafted", "sent", "replied", "agreed", "done")
WORK_STATUSES = ("shortlisted", "drafted", "sent", "replied", "agreed", "done")
LANG_FILTERS = ("all", "cyrillic", "latin", "cjk")
ACTIVITY_FILTERS = ("active", "all")
CSV_FIELDS = (
    "username",
    "title",
    "subscribers",
    "avg_views",
    "err",
    "status",
    "contact_status",
    "contact_value",
    "flags",
    "tab",
    "draft",
    "similarity",
    "size_fit",
    "score",
    "matched_topics",
)


def matched_topics(
    niche: NicheProfile | None, title: str | None, description: str | None, posts: list
) -> list[str]:
    if niche is None or not niche.terms:
        return []
    haystack = " ".join(
        [title or "", description or "", *(post.text or "" for post in posts)]
    ).lower()
    tokens = set(tokenize(haystack))
    matched = [
        term for term in niche.terms if (term in tokens if " " not in term else term in haystack)
    ]
    return matched[:3]


def tab_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"suitable": 0, "work": 0, "growing": 0, "hidden": 0}
    for row in rows:
        if row["tab"] == "hidden":
            counts["hidden"] += 1
        elif row["status"] in WORK_STATUSES:
            counts["work"] += 1
        elif row["tab"] == "suitable":
            counts["suitable"] += 1
        else:
            counts["growing"] += 1
    return counts


def visible_languages(profile: Profile | None) -> frozenset[str] | None:
    """The default language visibility set, or ``None`` for no filter.

    The owner's language plus English (``latin``) are visible, and rows whose
    language is ``unknown`` always stay visible. An owner whose language cannot
    be established (``other``/``unknown``) gets no language filter at all. CJK
    and other known scripts are hidden regardless of how much Latin they carry.
    """
    owner = owner_language(profile)
    if owner not in (CYRILLIC, LATIN, CJK):
        return None
    return frozenset({owner, LATIN, UNKNOWN})


def filter_by_language(
    rows: list[dict[str, Any]], lang: str | None, profile: Profile | None = None
) -> list[dict[str, Any]]:
    """Apply the language visibility filter.

    ``"all"`` keeps every row, a language code keeps that exact language, and
    the default (``None`` or empty) applies :func:`visible_languages`. Language
    never changes ranking: it only decides visibility.
    """
    if lang == "all":
        return rows
    if lang:
        return [row for row in rows if (row.get("lang") or UNKNOWN) == lang]
    allowed = visible_languages(profile)
    if allowed is None:
        return rows
    return [row for row in rows if (row.get("lang") or UNKNOWN) in allowed]


def filter_by_activity(
    rows: list[dict[str, Any]], activity: str | None
) -> list[dict[str, Any]]:
    """Hide ``inactive`` rows unless ``activity == "all"``.

    Rows without a known last post carry no ``inactive`` flag and stay visible.
    """
    if activity == "all":
        return rows
    return [row for row in rows if "inactive" not in row["flags"]]


def source_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source") or "")
        counts[source] = counts.get(source, 0) + 1
    return counts


def target_range(profile: Profile | None, settings: dict[str, str]) -> TargetRange | None:
    if profile is None:
        return None
    min_mult = get_float(settings, "min_mult")
    max_mult = get_float(settings, "max_mult")
    return derive_range(
        profile,
        min_mult if min_mult is not None else 0.3,
        max_mult if max_mult is not None else 3.0,
    )


def build_row(
    conn: sqlite3.Connection,
    profile: Profile | None,
    channel: Channel,
    settings: dict[str, str],
    niche: NicheProfile | None = None,
) -> dict[str, Any]:
    target = target_range(profile, settings)
    verdict = classify(channel, target) if target is not None else RangeVerdict("unknown_size")
    posts = get_posts(conn, channel.id, limit=20) if channel.id is not None else []
    flags = detect_flags(channel, posts, settings)
    if channel.status == "ignored":
        tab = "hidden"
    elif verdict.kind == "in_range":
        tab = "suitable"
    else:
        tab = "growing"
    draft = latest_draft(conn, channel.id) if channel.id is not None else None
    return {
        "id": channel.id,
        "username": channel.username,
        "title": channel.title,
        "subscribers": channel.subscribers,
        "avg_views": channel.avg_views,
        "err": channel.err,
        "status": channel.status,
        "contact_status": channel.contact_status,
        "contact_value": channel.contact_value,
        "lang": channel.lang,
        "flags": flags,
        "activity": (
            "inactive"
            if "inactive" in flags
            else ("active" if channel.last_post_at else "unknown")
        ),
        "tab": tab,
        "range_reason": verdict.kind,
        "source": channel.source,
        "last_post_at": channel.last_post_at,
        "draft": draft.text if draft is not None else "",
        "similarity": channel.similarity,
        "size_fit": channel.size_fit,
        "score": channel.score,
        "matched_topics": matched_topics(niche, channel.title, channel.description, posts),
    }


def build_rows(
    conn: sqlite3.Connection,
    profile: Profile | None,
    settings: dict[str, str],
    tab: str | None = None,
    status: str | None = None,
    contact: str | None = None,
    source: str | None = None,
    flag: str | None = None,
    q: str | None = None,
    lang: str | None = None,
    activity: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    channels = list_channels(conn, status=status, contact=contact, source=source, q=q)
    niche = build_niche_profile(profile) if profile is not None else None
    rows = [build_row(conn, profile, channel, settings, niche=niche) for channel in channels]
    if tab and tab != "all":
        if tab == "work":
            rows = [row for row in rows if row["status"] in WORK_STATUSES]
        else:
            rows = [row for row in rows if row["tab"] == tab]
    if flag:
        rows = [row for row in rows if flag in row["flags"]]
    rows = filter_by_language(rows, lang, profile)
    rows = filter_by_activity(rows, activity)
    if limit is not None:
        rows = rows[: int(limit)]
    return rows


def _label(group: str, value: str, lang: str) -> str:
    # Local import: the web package initialises the app, which imports reports.
    from tg_collab.web.i18n import label

    return label(group, value, lang)


def _contact_text(row: dict[str, Any], lang: str = "en") -> str:
    text = _label("contact", row["contact_status"], lang)
    if row["contact_status"] == "comments_open":
        return text
    return f"{text} ({row['contact_value']})" if row["contact_value"] else text


def export_markdown(rows: list[dict[str, Any]], lang: str = "en") -> str:
    lines = ["# Outreach", ""]
    for status in STATUS_ORDER:
        group = [row for row in rows if row["status"] == status]
        if not group:
            continue
        lines.append(f"## {_label('status', status, lang)}")
        lines.append("")
        for row in group:
            title = row["title"] or row["username"]
            lines.append(f"### @{row['username']} - {title}")
            lines.append("")
            lines.append(f"- Subscribers: {row['subscribers'] if row['subscribers'] is not None else '-'}")
            lines.append(f"- Average views: {row['avg_views'] if row['avg_views'] is not None else '-'}")
            flags = ", ".join(_label("flag", flag, lang) for flag in row["flags"])
            lines.append(f"- Flags: {flags if flags else '-'}")
            lines.append(f"- Contact: {_contact_text(row, lang)}")
            lines.append(f"- Range: {_label('range', row['range_reason'], lang)}")
            lines.append(f"- Similarity: {row['similarity'] if row['similarity'] is not None else '-'}")
            lines.append(f"- Size fit: {row['size_fit'] if row['size_fit'] is not None else '-'}")
            lines.append(f"- Score: {row['score'] if row['score'] is not None else '-'}")
            lines.append(
                f"- Topics: {', '.join(row['matched_topics']) if row['matched_topics'] else '-'}"
            )
            if row.get("draft"):
                lines.append("")
                lines.append("Draft:")
                lines.append("")
                lines.extend(f"> {line}" if line else ">" for line in row["draft"].splitlines())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        payload = dict(row)
        payload["flags"] = "|".join(row["flags"])
        payload["matched_topics"] = "|".join(row["matched_topics"])
        writer.writerow(payload)
    return buffer.getvalue()
