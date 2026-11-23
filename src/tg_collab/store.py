"""SQLite repositories. Parameterized SQL only; all writes go through these functions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from tg_collab.language import detect_language
from tg_collab.models import (
    STATUSES,
    Channel,
    ChannelPost,
    Draft,
    Job,
    Lead,
    Profile,
    utcnow,
)


def _channel_from_row(row: sqlite3.Row) -> Channel:
    return Channel(
        id=row["id"],
        profile_id=row["profile_id"],
        username=row["username"],
        source=row["source"],
        title=row["title"],
        description=row["description"],
        lang=row["lang"],
        subscribers=row["subscribers"],
        avg_views=row["avg_views"],
        median_views=row["median_views"],
        err=row["err"],
        posts_per_week=row["posts_per_week"],
        last_post_at=row["last_post_at"],
        contact_status=row["contact_status"],
        contact_value=row["contact_value"],
        status=row["status"],
        similarity=row["similarity"],
        size_fit=row["size_fit"],
        score=row["score"],
        flags=json.loads(row["flags_json"] or "[]"),
        meta=json.loads(row["meta_json"] or "{}"),
        notes=row["notes"],
        discovered_at=row["discovered_at"],
        updated_at=row["updated_at"],
        checked_at=row["checked_at"],
    )


def _post_from_row(row: sqlite3.Row) -> ChannelPost:
    return ChannelPost(
        message_id=row["message_id"],
        posted_at=row["posted_at"],
        views=row["views"],
        text=row["text"],
    )


def _draft_from_row(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        channel_id=row["channel_id"],
        text=row["text"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        progress=row["progress"],
        message=row["message"],
        log=row["log"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        profile_id=row["profile_id"],
    )


DEFAULT_PROFILE_ID = 1


def _profile_from_row(row: sqlite3.Row) -> Profile:
    return Profile(
        id=row["id"],
        username=row["username"],
        title=row["title"],
        description=row["description"],
        subscribers=row["subscribers"],
        avg_views=row["avg_views"],
        median_views=row["median_views"],
        err=row["err"],
        posts_per_week=row["posts_per_week"],
        links=json.loads(row["links_json"] or "[]"),
        pinned_text=row["pinned_text"],
        last_post_at=row["last_post_at"],
        tgstat=json.loads(row["tgstat_json"]) if row["tgstat_json"] else None,
        posts_json=row["posts_json"] or "[]",
        niche_json=row["niche_json"] or "",
        seed_channels=row["seed_channels"] or "",
        fetched_at=row["fetched_at"],
    )


def _clean_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@").lower()


def active_profile_id(conn: sqlite3.Connection) -> int | None:
    """The stored active profile when it still exists, else the first profile."""
    row = conn.execute("SELECT value FROM settings WHERE key='active_profile_id'").fetchone()
    if row is not None:
        raw = str(row[0] or "").strip()
        if raw:
            try:
                candidate = int(raw)
            except ValueError:
                candidate = None
            if candidate is not None and conn.execute(
                "SELECT 1 FROM profiles WHERE id=?", (candidate,)
            ).fetchone() is not None:
                return candidate
    row = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
    return int(row[0]) if row is not None else None


def profile_scope(conn: sqlite3.Connection, profile_id: int | None = None) -> int:
    if profile_id is not None:
        return int(profile_id)
    active = active_profile_id(conn)
    return active if active is not None else DEFAULT_PROFILE_ID


def list_profiles(conn: sqlite3.Connection) -> list[Profile]:
    return [
        _profile_from_row(row)
        for row in conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    ]


def get_profile_by_id(conn: sqlite3.Connection, profile_id: int) -> Profile | None:
    row = conn.execute("SELECT * FROM profiles WHERE id=?", (int(profile_id),)).fetchone()
    return _profile_from_row(row) if row is not None else None


def get_profile(conn: sqlite3.Connection) -> Profile | None:
    """The active profile, falling back to the first one; ``None`` when there are none."""
    profile_id = active_profile_id(conn)
    return get_profile_by_id(conn, profile_id) if profile_id is not None else None


def create_profile(conn: sqlite3.Connection, username: str) -> Profile:
    name = _clean_username(username)
    if not name:
        raise ValueError("profile username is required")
    if conn.execute(
        "SELECT 1 FROM profiles WHERE username=? COLLATE NOCASE", (name,)
    ).fetchone() is not None:
        raise ValueError(f"profile already exists: @{name}")
    now = utcnow()
    with conn:
        cursor = conn.execute(
            "INSERT INTO profiles(username, fetched_at, created_at, seed_channels)"
            " VALUES (?, ?, ?, '')",
            (name, now, now),
        )
    profile_id = int(cursor.lastrowid or 0)
    if active_profile_id(conn) is None:
        set_active_profile(conn, profile_id)
    stored = get_profile_by_id(conn, profile_id)
    assert stored is not None
    return stored


def set_active_profile(conn: sqlite3.Connection, profile_id: int) -> Profile:
    profile = get_profile_by_id(conn, profile_id)
    if profile is None:
        raise LookupError(f"profile not found: {profile_id}")
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES ('active_profile_id', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (str(profile.id), utcnow()),
        )
    return profile


def delete_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    profile = get_profile_by_id(conn, profile_id)
    if profile is None:
        raise LookupError(f"profile not found: {profile_id}")
    if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] <= 1:
        raise ValueError("cannot delete the last profile")
    with conn:
        conn.execute("DELETE FROM channels WHERE profile_id=?", (int(profile_id),))
        conn.execute("DELETE FROM leads WHERE profile_id=?", (int(profile_id),))
        conn.execute("DELETE FROM jobs WHERE profile_id=?", (int(profile_id),))
        conn.execute("DELETE FROM profiles WHERE id=?", (int(profile_id),))
    row = conn.execute("SELECT value FROM settings WHERE key='active_profile_id'").fetchone()
    stored_id: int | None = None
    if row is not None and str(row[0] or "").strip().isdigit():
        stored_id = int(str(row[0]).strip())
    if stored_id is not None and get_profile_by_id(conn, stored_id) is not None:
        return
    remaining = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
    if remaining is not None:
        set_active_profile(conn, int(remaining[0]))


def upsert_profile(
    conn: sqlite3.Connection, profile: Profile, profile_id: int | None = None
) -> Profile:
    """Write the fetched channel into ``profile_id`` (default: the active profile).

    Creates the first profile when none exists; ``niche_json`` is never touched
    by an update, so re-running ``init`` keeps the stored niche.
    """
    target_id = profile_id if profile_id is not None else active_profile_id(conn)
    name = _clean_username(profile.username)
    if not name:
        raise ValueError("profile username is required")
    clash = conn.execute(
        "SELECT id FROM profiles WHERE username=? COLLATE NOCASE AND id IS NOT ?",
        (name, target_id if target_id is not None else -1),
    ).fetchone()
    if clash is not None:
        raise ValueError(
            f"another profile already uses @{name}; run `tg-collab profile use @{name}` to switch"
        )
    links_json = json.dumps(profile.links, ensure_ascii=False)
    tgstat_json = json.dumps(profile.tgstat, ensure_ascii=False) if profile.tgstat is not None else None
    fetched_at = profile.fetched_at or utcnow()
    values = (
        name,
        profile.title,
        profile.description,
        profile.subscribers,
        profile.avg_views,
        profile.median_views,
        profile.err,
        profile.posts_per_week,
        links_json,
        profile.pinned_text,
        profile.last_post_at,
        tgstat_json,
        fetched_at,
        profile.posts_json or "[]",
    )
    if target_id is None:
        with conn:
            cursor = conn.execute(
                "INSERT INTO profiles(username, title, description, subscribers, avg_views,"
                " median_views, err, posts_per_week, links_json, pinned_text, last_post_at,"
                " tgstat_json, fetched_at, posts_json, niche_json, seed_channels,"
                " created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *values,
                    profile.niche_json or "",
                    profile.seed_channels or "",
                    utcnow(),
                ),
            )
        target_id = int(cursor.lastrowid or 0)
        set_active_profile(conn, target_id)
    else:
        with conn:
            conn.execute(
                "UPDATE profiles SET username=?, title=?, description=?, subscribers=?,"
                " avg_views=?, median_views=?, err=?, posts_per_week=?, links_json=?,"
                " pinned_text=?, last_post_at=?, tgstat_json=?, fetched_at=?, posts_json=?"
                " WHERE id=?",
                (*values, target_id),
            )
    stored = get_profile_by_id(conn, target_id)
    if stored is None:
        raise LookupError(f"profile not found: {target_id}")
    return stored


def set_profile_niche(
    conn: sqlite3.Connection, value: str, profile_id: int | None = None
) -> None:
    pid = profile_scope(conn, profile_id)
    with conn:
        conn.execute("UPDATE profiles SET niche_json=? WHERE id=?", (value or "", pid))


def set_profile_seed_channels(
    conn: sqlite3.Connection, value: str, profile_id: int | None = None
) -> None:
    pid = profile_scope(conn, profile_id)
    with conn:
        conn.execute("UPDATE profiles SET seed_channels=? WHERE id=?", (value or "", pid))


def rename_profile(
    conn: sqlite3.Connection, username: str, profile_id: int | None = None
) -> Profile:
    """Rename one profile; a duplicate username is a ``ValueError``."""
    name = _clean_username(username)
    if not name:
        raise ValueError("profile username is required")
    pid = profile_scope(conn, profile_id)
    if get_profile_by_id(conn, pid) is None:
        raise LookupError(f"profile not found: {pid}")
    clash = conn.execute(
        "SELECT id FROM profiles WHERE username=? COLLATE NOCASE AND id IS NOT ?",
        (name, pid),
    ).fetchone()
    if clash is not None:
        raise ValueError(f"another profile already uses @{name}")
    with conn:
        conn.execute("UPDATE profiles SET username=? WHERE id=?", (name, pid))
    stored = get_profile_by_id(conn, pid)
    assert stored is not None
    return stored


# --- channels --------------------------------------------------------------


def upsert_channel(
    conn: sqlite3.Connection, channel: Channel, profile_id: int | None = None
) -> Channel:
    """Insert or update by (profile, username); status, notes, and discovered_at are preserved.

    ``lang`` falls back to a detection over the title and description when the
    caller has none. A blank ``lang`` on update keeps the stored value, so a
    re-enrichment without text cannot wipe a known language.
    """
    pid = profile_scope(conn, profile_id)
    now = utcnow()
    discovered_at = channel.discovered_at or now
    lang = channel.lang
    if not lang and (channel.title or channel.description):
        lang = detect_language(channel.title, channel.description)
    with conn:
        conn.execute(
            """
            INSERT INTO channels(profile_id, username, title, description, lang, subscribers,
                                 avg_views, median_views, err, posts_per_week, last_post_at,
                                 contact_status, contact_value, status, source,
                                 similarity, size_fit, score,
                                 flags_json, meta_json, notes, discovered_at,
                                 updated_at, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, username) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                lang=CASE WHEN excluded.lang = '' THEN channels.lang ELSE excluded.lang END,
                subscribers=excluded.subscribers,
                avg_views=excluded.avg_views,
                median_views=excluded.median_views,
                err=excluded.err,
                posts_per_week=excluded.posts_per_week,
                last_post_at=excluded.last_post_at,
                contact_status=excluded.contact_status,
                contact_value=excluded.contact_value,
                similarity=excluded.similarity,
                size_fit=excluded.size_fit,
                score=excluded.score,
                flags_json=excluded.flags_json,
                meta_json=excluded.meta_json,
                updated_at=excluded.updated_at,
                checked_at=excluded.checked_at
            """,
            (
                pid,
                channel.username,
                channel.title,
                channel.description,
                lang,
                channel.subscribers,
                channel.avg_views,
                channel.median_views,
                channel.err,
                channel.posts_per_week,
                channel.last_post_at,
                channel.contact_status,
                channel.contact_value,
                channel.status,
                channel.source,
                channel.similarity,
                channel.size_fit,
                channel.score,
                json.dumps(channel.flags, ensure_ascii=False),
                json.dumps(channel.meta, ensure_ascii=False),
                channel.notes,
                discovered_at,
                now,
                channel.checked_at,
            ),
        )
    stored = get_channel(conn, username=channel.username, profile_id=pid)
    assert stored is not None
    return stored


def get_channel(
    conn: sqlite3.Connection,
    username: str | None = None,
    channel_id: int | None = None,
    profile_id: int | None = None,
) -> Channel | None:
    pid = profile_scope(conn, profile_id)
    if channel_id is not None:
        row = conn.execute(
            "SELECT * FROM channels WHERE id=? AND profile_id=?", (channel_id, pid)
        ).fetchone()
    elif username is not None:
        row = conn.execute(
            "SELECT * FROM channels WHERE username=? COLLATE NOCASE AND profile_id=?",
            (username.lstrip("@"), pid),
        ).fetchone()
    else:
        raise ValueError("get_channel needs a username or a channel_id")
    return _channel_from_row(row) if row is not None else None


def list_channels(
    conn: sqlite3.Connection,
    status: str | None = None,
    contact: str | None = None,
    source: str | None = None,
    flag: str | None = None,
    q: str | None = None,
    limit: int | None = None,
    profile_id: int | None = None,
) -> list[Channel]:
    clauses: list[str] = ["profile_id=?"]
    params: list[Any] = [profile_scope(conn, profile_id)]
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if contact is not None:
        clauses.append("contact_status=?")
        params.append(contact)
    if source is not None:
        clauses.append("source=?")
        params.append(source)
    if q:
        clauses.append("(username LIKE ? OR title LIKE ? OR description LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    sql = "SELECT * FROM channels WHERE " + " AND ".join(clauses)
    sql += " ORDER BY score IS NULL, score DESC, subscribers IS NULL, subscribers DESC, username"
    if limit is not None and not flag:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    channels = [_channel_from_row(row) for row in rows]
    if flag:
        channels = [channel for channel in channels if flag in channel.flags]
    if limit is not None:
        channels = channels[: int(limit)]
    return channels


def backfill_channel_languages(conn: sqlite3.Connection, posts_per_channel: int = 30) -> int:
    """Fill ``lang`` for rows that never had one, from stored title/description/posts.

    Only rows with an empty ``lang`` are touched, so a second run is a no-op and
    a non-empty language is never recomputed. Returns the number of rows updated.
    """
    rows = conn.execute("SELECT id, title, description FROM channels WHERE lang=''").fetchall()
    updated = 0
    with conn:
        for row in rows:
            posts = get_posts(conn, row["id"], limit=posts_per_channel)
            lang = detect_language(
                row["title"], row["description"], *(post.text for post in posts)
            )
            cursor = conn.execute(
                "UPDATE channels SET lang=? WHERE id=? AND lang=''", (lang, row["id"])
            )
            updated += cursor.rowcount
    return updated


def set_status(
    conn: sqlite3.Connection, username: str, status: str, profile_id: int | None = None
) -> Channel:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    pid = profile_scope(conn, profile_id)
    with conn:
        cursor = conn.execute(
            "UPDATE channels SET status=?, updated_at=? "
            "WHERE username=? COLLATE NOCASE AND profile_id=?",
            (status, utcnow(), username.lstrip("@"), pid),
        )
    if cursor.rowcount == 0:
        raise LookupError(f"channel not found: @{username.lstrip('@')}")
    stored = get_channel(conn, username=username, profile_id=pid)
    assert stored is not None
    return stored


def set_notes(
    conn: sqlite3.Connection, username: str, notes: str, profile_id: int | None = None
) -> Channel:
    pid = profile_scope(conn, profile_id)
    with conn:
        cursor = conn.execute(
            "UPDATE channels SET notes=?, updated_at=? "
            "WHERE username=? COLLATE NOCASE AND profile_id=?",
            (notes, utcnow(), username.lstrip("@"), pid),
        )
    if cursor.rowcount == 0:
        raise LookupError(f"channel not found: @{username.lstrip('@')}")
    stored = get_channel(conn, username=username, profile_id=pid)
    assert stored is not None
    return stored


# --- leads -----------------------------------------------------------------


def _lead_from_row(row: sqlite3.Row) -> Lead:
    return Lead(
        id=row["id"],
        profile_id=row["profile_id"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        subscribers=row["subscribers"],
        source=row["source"],
        note=row["note"],
        created_at=row["created_at"],
    )


def upsert_lead(
    conn: sqlite3.Connection, lead: Lead, profile_id: int | None = None
) -> Lead:
    """Insert or update by (profile, URL). A NULL field keeps the stored value."""
    url = (lead.url or "").strip()
    if not url:
        raise ValueError("lead url is required")
    pid = profile_scope(conn, profile_id)
    created_at = lead.created_at or utcnow()
    with conn:
        conn.execute(
            """
            INSERT INTO leads(profile_id, url, title, description, subscribers, source, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, url) DO UPDATE SET
                title=COALESCE(excluded.title, leads.title),
                description=COALESCE(excluded.description, leads.description),
                subscribers=COALESCE(excluded.subscribers, leads.subscribers),
                source=COALESCE(excluded.source, leads.source),
                note=COALESCE(excluded.note, leads.note)
            """,
            (pid, url, lead.title, lead.description, lead.subscribers, lead.source, lead.note, created_at),
        )
    stored = get_lead_by_url(conn, url, profile_id=pid)
    assert stored is not None
    return stored


def get_lead_by_url(
    conn: sqlite3.Connection, url: str, profile_id: int | None = None
) -> Lead | None:
    row = conn.execute(
        "SELECT * FROM leads WHERE url=? AND profile_id=?",
        ((url or "").strip(), profile_scope(conn, profile_id)),
    ).fetchone()
    return _lead_from_row(row) if row is not None else None


def list_leads(
    conn: sqlite3.Connection, limit: int | None = None, profile_id: int | None = None
) -> list[Lead]:
    sql = "SELECT * FROM leads WHERE profile_id=? ORDER BY id DESC"
    params: list[Any] = [profile_scope(conn, profile_id)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_lead_from_row(row) for row in conn.execute(sql, params).fetchall()]


# --- posts -----------------------------------------------------------------


def replace_posts(
    conn: sqlite3.Connection,
    channel_id: int,
    posts: Iterable[ChannelPost],
    keep: int,
) -> int:
    """Replace stored posts for a channel, retaining the newest ``keep`` by message id."""
    with conn:
        conn.execute("DELETE FROM channel_posts WHERE channel_id=?", (channel_id,))
        for post in posts:
            conn.execute(
                "INSERT OR IGNORE INTO channel_posts(channel_id, message_id, posted_at, views, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_id, post.message_id, post.posted_at, post.views, post.text),
            )
        if keep > 0:
            conn.execute(
                "DELETE FROM channel_posts WHERE channel_id=? AND id NOT IN ("
                "SELECT id FROM channel_posts WHERE channel_id=? ORDER BY message_id DESC LIMIT ?)",
                (channel_id, channel_id, int(keep)),
            )
        else:
            conn.execute("DELETE FROM channel_posts WHERE channel_id=?", (channel_id,))
    return conn.execute(
        "SELECT COUNT(*) FROM channel_posts WHERE channel_id=?", (channel_id,)
    ).fetchone()[0]


def get_posts(conn: sqlite3.Connection, channel_id: int, limit: int = 20) -> list[ChannelPost]:
    rows = conn.execute(
        "SELECT * FROM channel_posts WHERE channel_id=? ORDER BY message_id DESC LIMIT ?",
        (channel_id, int(limit)),
    ).fetchall()
    return [_post_from_row(row) for row in rows]


# --- drafts ----------------------------------------------------------------


def save_draft(conn: sqlite3.Connection, channel_id: int, text: str) -> Draft:
    now = utcnow()
    with conn:
        cursor = conn.execute(
            "INSERT INTO drafts(channel_id, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (channel_id, text, now, now),
        )
    draft_id = int(cursor.lastrowid or 0)
    stored = get_draft(conn, draft_id)
    assert stored is not None
    return stored


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Draft | None:
    row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    return _draft_from_row(row) if row is not None else None


def latest_draft(conn: sqlite3.Connection, channel_id: int) -> Draft | None:
    row = conn.execute(
        "SELECT * FROM drafts WHERE channel_id=? ORDER BY id DESC LIMIT 1", (channel_id,)
    ).fetchone()
    return _draft_from_row(row) if row is not None else None


# --- jobs ------------------------------------------------------------------

_JOB_COLUMNS = {"status", "progress", "message", "log", "result_json", "finished_at"}
_TERMINAL = {"done", "error", "cancelled"}


def create_job(conn: sqlite3.Connection, kind: str, profile_id: int | None = None) -> Job:
    job_id = uuid.uuid4().hex
    now = utcnow()
    pid = profile_id if profile_id is not None else active_profile_id(conn)
    with conn:
        conn.execute(
            "INSERT INTO jobs(id, kind, status, created_at, updated_at, profile_id) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (job_id, kind, now, now, pid),
        )
    stored = get_job(conn, job_id)
    assert stored is not None
    return stored


def update_job(conn: sqlite3.Connection, job_id: str, **fields: Any) -> Job:
    unknown = set(fields) - _JOB_COLUMNS
    if unknown:
        raise ValueError(f"unknown job field(s): {', '.join(sorted(unknown))}")
    values: dict[str, Any] = dict(fields)
    status = values.get("status")
    if status is not None and status not in ("running", "done", "error", "cancelled"):
        raise ValueError(f"unknown job status: {status}")
    if status in _TERMINAL and "finished_at" not in values:
        values["finished_at"] = utcnow()
    values["updated_at"] = utcnow()
    assignments = ", ".join(f"{name}=?" for name in values)
    with conn:
        conn.execute(
            f"UPDATE jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )
    stored = get_job(conn, job_id)
    if stored is None:
        raise LookupError(f"job not found: {job_id}")
    return stored


def get_job(conn: sqlite3.Connection, job_id: str) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_from_row(row) if row is not None else None


def latest_running_job(
    conn: sqlite3.Connection, kind: str | None = None, profile_id: int | None = None
) -> Job | None:
    """The newest job that is still running, optionally of one kind and profile."""
    query = "SELECT * FROM jobs WHERE status='running'"
    params: list[Any] = []
    if kind is not None:
        query += " AND kind=?"
        params.append(kind)
    if profile_id is not None:
        query += " AND profile_id=?"
        params.append(int(profile_id))
    row = conn.execute(query + " ORDER BY rowid DESC LIMIT 1", params).fetchone()
    return _job_from_row(row) if row is not None else None


# --- search cache ----------------------------------------------------------


def get_search_cache(
    conn: sqlite3.Connection,
    query: str,
    engine: str,
    region: str,
    max_age_days: int,
    profile_fetched_at: str | None,
) -> list[str] | None:
    row = conn.execute(
        "SELECT results_json, fetched_at, profile_fetched_at FROM search_cache "
        "WHERE query=? AND engine=? AND region=?",
        (query, engine, region),
    ).fetchone()
    if row is None or row["profile_fetched_at"] != profile_fetched_at:
        return None
    try:
        fetched = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched > timedelta(days=max_age_days):
        return None
    value = json.loads(row["results_json"])
    return [str(item) for item in value] if isinstance(value, list) else None


def put_search_cache(
    conn: sqlite3.Connection,
    query: str,
    engine: str,
    region: str,
    results: list[str],
    max_age_days: int,
    profile_fetched_at: str | None,
) -> None:
    now = utcnow()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM search_cache WHERE fetched_at < ?", (cutoff,))
        conn.execute(
            "INSERT INTO search_cache(query, engine, region, results_json, fetched_at, profile_fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(query, engine, region) DO UPDATE SET "
            "results_json=excluded.results_json, fetched_at=excluded.fetched_at, "
            "profile_fetched_at=excluded.profile_fetched_at",
            (query, engine, region, json.dumps(results, ensure_ascii=False), now, profile_fetched_at),
        )
