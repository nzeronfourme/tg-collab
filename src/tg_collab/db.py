"""SQLite connection policy, schema, and migrations."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from tg_collab.config import SETTING_DEFAULTS
from tg_collab.language import detect_language
from tg_collab.models import utcnow

SCHEMA_VERSION = 8
LANGUAGE_RECOMPUTE_POSTS = 30
EXTRA_TERM_SPLIT_RE = re.compile(r"[,;\n]+")
EXTRA_TERM_LIMIT = 12

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK(id=1),
    username TEXT NOT NULL,
    title TEXT,
    description TEXT,
    subscribers INTEGER,
    avg_views INTEGER,
    median_views INTEGER,
    err REAL,
    posts_per_week REAL,
    links_json TEXT NOT NULL DEFAULT '[]',
    pinned_text TEXT,
    last_post_at TEXT,
    tgstat_json TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    title TEXT,
    description TEXT,
    subscribers INTEGER,
    avg_views INTEGER,
    median_views INTEGER,
    err REAL,
    posts_per_week REAL,
    last_post_at TEXT,
    contact_status TEXT NOT NULL DEFAULT 'manual_needed'
        CHECK(contact_status IN ('direct_admin','bot_or_suggest','comments_open','manual_needed')),
    contact_value TEXT,
    status TEXT NOT NULL DEFAULT 'found'
        CHECK(status IN ('found','shortlisted','drafted','sent','replied','agreed','done','ignored')),
    source TEXT NOT NULL,
    flags_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    posted_at TEXT NOT NULL,
    views INTEGER,
    text TEXT,
    UNIQUE(channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running','done','error','cancelled')),
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
"""

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if table not in _tables(conn):
        return
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Similarity ranking, the niche corpus, and the search cache. Idempotent."""
    _add_column(conn, "channels", "similarity", "INTEGER")
    _add_column(conn, "channels", "size_fit", "REAL")
    _add_column(conn, "channels", "score", "REAL")
    _add_column(conn, "profile", "posts_json", "TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            engine TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT '',
            results_json TEXT NOT NULL DEFAULT '[]',
            fetched_at TEXT NOT NULL,
            profile_fetched_at TEXT,
            UNIQUE(query, engine, region)
        )
        """
    )
    placeholders = ", ".join("?" for _ in SETTING_DEFAULTS)
    conn.execute(
        f"DELETE FROM settings WHERE key NOT IN ({placeholders})", tuple(SETTING_DEFAULTS)
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Channel language and invite-only leads. Idempotent."""
    _add_column(conn, "channels", "lang", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            description TEXT,
            subscribers INTEGER,
            source TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """The niche profile column and a forced language recompute.

    Every stored channel is re-detected from its title, description, and up to
    ``LANGUAGE_RECOMPUTE_POSTS`` stored posts with the current detector, so an
    upgraded database never keeps a language the new CJK rule would change.
    Re-running is a data no-op: the detector is deterministic.
    """
    _add_column(conn, "profile", "niche_json", "TEXT NOT NULL DEFAULT ''")
    rows = conn.execute("SELECT id, title, description FROM channels").fetchall()
    with conn:
        for row in rows:
            posts = conn.execute(
                "SELECT text FROM channel_posts WHERE channel_id=? "
                "ORDER BY message_id DESC LIMIT ?",
                (row["id"], LANGUAGE_RECOMPUTE_POSTS),
            ).fetchall()
            lang = detect_language(
                row["title"], row["description"], *(post["text"] for post in posts)
            )
            conn.execute("UPDATE channels SET lang=? WHERE id=?", (lang, row["id"]))


PROFILES_V5 = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    title TEXT,
    description TEXT,
    subscribers INTEGER,
    avg_views INTEGER,
    median_views INTEGER,
    err REAL,
    posts_per_week REAL,
    links_json TEXT NOT NULL DEFAULT '[]',
    pinned_text TEXT,
    last_post_at TEXT,
    tgstat_json TEXT,
    fetched_at TEXT NOT NULL,
    posts_json TEXT NOT NULL DEFAULT '[]',
    niche_json TEXT NOT NULL DEFAULT '',
    seed_channels TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

#: Rebuilt because SQLite cannot change a table-level UNIQUE in place.
CHANNELS_V5 = """
CREATE TABLE channels_v5 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL DEFAULT 1,
    username TEXT NOT NULL COLLATE NOCASE,
    title TEXT,
    description TEXT,
    subscribers INTEGER,
    avg_views INTEGER,
    median_views INTEGER,
    err REAL,
    posts_per_week REAL,
    last_post_at TEXT,
    contact_status TEXT NOT NULL DEFAULT 'manual_needed'
        CHECK(contact_status IN ('direct_admin','bot_or_suggest','comments_open','manual_needed')),
    contact_value TEXT,
    status TEXT NOT NULL DEFAULT 'found'
        CHECK(status IN ('found','shortlisted','drafted','sent','replied','agreed','done','ignored')),
    source TEXT NOT NULL,
    flags_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    checked_at TEXT,
    similarity INTEGER,
    size_fit REAL,
    score REAL,
    lang TEXT NOT NULL DEFAULT '',
    UNIQUE(profile_id, username)
);
"""

LEADS_V5 = """
CREATE TABLE leads_v5 (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL DEFAULT 1,
    url TEXT NOT NULL,
    title TEXT,
    description TEXT,
    subscribers INTEGER,
    source TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, url)
);
"""

_CHANNEL_COPY_COLUMNS = (
    "id", "profile_id", "username", "title", "description", "subscribers", "avg_views",
    "median_views", "err", "posts_per_week", "last_post_at", "contact_status",
    "contact_value", "status", "source", "flags_json", "meta_json", "notes",
    "discovered_at", "updated_at", "checked_at", "similarity", "size_fit", "score", "lang",
)
_LEAD_COPY_COLUMNS = (
    "id", "profile_id", "url", "title", "description", "subscribers", "source", "note",
    "created_at",
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _rebuild_v5(conn: sqlite3.Connection, table: str, ddl: str, columns: tuple[str, ...]) -> None:
    """Recreate ``table`` with the v5 DDL, keeping ids and every existing column."""
    temp = f"{table}_v5"
    conn.execute(f"DROP TABLE IF EXISTS {temp}")
    conn.execute(ddl)
    select = ", ".join("1" if column == "profile_id" else column for column in columns)
    conn.execute(
        f"INSERT INTO {temp}({', '.join(columns)}) SELECT {select} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {temp} RENAME TO {table}")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Several profiles, each with its own channels, leads, and jobs. Idempotent.

    The single ``profile`` row becomes ``profiles`` id 1 and becomes active;
    ``channels`` and ``leads`` are rebuilt so their uniqueness is per profile.
    Re-running finds no legacy table and no missing column, so it writes nothing.
    """
    conn.commit()
    # DROP TABLE with enforcement on would cascade into channel_posts/drafts.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute(PROFILES_V5)
            if "profile" in _tables(conn):
                legacy_columns = _columns(conn, "profile")
                row = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
                empty = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
                if row is not None and empty:
                    fetched_at = row["fetched_at"] or utcnow()

                    def legacy(name: str, default: str) -> str:
                        return (
                            str(row[name])
                            if name in legacy_columns and row[name] is not None
                            else default
                        )

                    conn.execute(
                        "INSERT INTO profiles(id, username, title, description, subscribers,"
                        " avg_views, median_views, err, posts_per_week, links_json, pinned_text,"
                        " last_post_at, tgstat_json, fetched_at, posts_json, niche_json, created_at)"
                        " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["username"],
                            row["title"],
                            row["description"],
                            row["subscribers"],
                            row["avg_views"],
                            row["median_views"],
                            row["err"],
                            row["posts_per_week"],
                            row["links_json"],
                            row["pinned_text"],
                            row["last_post_at"],
                            row["tgstat_json"],
                            fetched_at,
                            legacy("posts_json", "[]"),
                            legacy("niche_json", ""),
                            fetched_at,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO settings(key, value, updated_at) "
                        "VALUES ('active_profile_id','1',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        (utcnow(),),
                    )
                conn.execute("DROP TABLE profile")
            if "profile_id" not in _columns(conn, "channels"):
                _rebuild_v5(conn, "channels", CHANNELS_V5, _CHANNEL_COPY_COLUMNS)
            if "profile_id" not in _columns(conn, "leads"):
                _rebuild_v5(conn, "leads", LEADS_V5, _LEAD_COPY_COLUMNS)
            _add_column(conn, "jobs", "profile_id", "INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_profile ON jobs(profile_id)")
            dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
            if dangling:
                raise sqlite3.IntegrityError(
                    f"migration v5 left dangling references: {dangling[:3]}"
                )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Per-profile seed channels; the legacy global value seeds the first profile. Idempotent."""
    with conn:
        _add_column(conn, "profiles", "seed_channels", "TEXT NOT NULL DEFAULT ''")
        if "profiles" not in _tables(conn):
            return
        row = conn.execute("SELECT value FROM settings WHERE key='seed_channels'").fetchone()
        legacy = str(row[0] or "").strip() if row is not None else ""
        if not legacy:
            return
        first = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
        if first is not None:
            conn.execute(
                "UPDATE profiles SET seed_channels=? WHERE id=? AND seed_channels=''",
                (legacy, first[0]),
            )


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """Owner-provided extra topics per profile. Idempotent."""
    with conn:
        _add_column(conn, "profiles", "extra_terms", "TEXT NOT NULL DEFAULT ''")


def _split_extra_terms(raw: str | None) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for piece in EXTRA_TERM_SPLIT_RE.split(raw or ""):
        term = " ".join(piece.split()).lower()
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms[:EXTRA_TERM_LIMIT]


def _merge_extra_terms(niche_json: str, extras: list[str]) -> str:
    """Append the owner topics to a stored niche, keeping its other sections."""
    payload: dict = {}
    if (niche_json or "").strip():
        try:
            loaded = json.loads(niche_json)
        except (TypeError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded
    terms: list[str] = []
    seen: set[str] = set()
    stored_terms = payload.get("terms")
    if not isinstance(stored_terms, list):
        stored_terms = []
    for term in (*stored_terms, *extras):
        if not isinstance(term, str):
            continue
        text = " ".join(term.split()).lower()
        if text and text not in seen:
            seen.add(text)
            terms.append(text)
    payload["terms"] = terms[:EXTRA_TERM_LIMIT]
    payload["source"] = "manual"
    payload["generated_at"] = utcnow()
    return json.dumps(payload, ensure_ascii=False)


def _drop_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Drop a plain column when the SQLite build supports ``DROP COLUMN``."""
    if column not in _columns(conn, table):
        return False
    if sqlite3.sqlite_version_info < (3, 35, 0):
        return False
    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    return True


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """Fold owner extra topics into the niche terms and drop the column. Idempotent.

    The tag editor owns the niche now, so an upgraded database keeps the old
    topics as manual niche terms instead of a separate field. A second run finds
    no column and writes nothing.
    """
    with conn:
        if "profiles" not in _tables(conn) or "extra_terms" not in _columns(conn, "profiles"):
            return
        rows = conn.execute("SELECT id, niche_json, extra_terms FROM profiles").fetchall()
        for row in rows:
            extras = _split_extra_terms(row["extra_terms"])
            if not extras:
                continue
            conn.execute(
                "UPDATE profiles SET niche_json=? WHERE id=?",
                (_merge_extra_terms(row["niche_json"], extras), row["id"]),
            )
        _drop_column(conn, "profiles", "extra_terms")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the project-wide pragmas applied."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the database directory if needed, apply migrations, return the connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        conn.executescript(SCHEMA_V1)
        version = 1
    if version < 2:
        _migrate_v2(conn)
        version = 2
    if version < 3:
        _migrate_v3(conn)
        version = 3
    if version < 4:
        _migrate_v4(conn)
        version = 4
    if version < 5:
        _migrate_v5(conn)
        version = 5
    if version < 6:
        _migrate_v6(conn)
        version = 6
    if version < 7:
        _migrate_v7(conn)
        version = 7
    if version < 8:
        _migrate_v8(conn)
        version = 8
    if version != int(conn.execute("PRAGMA user_version").fetchone()[0]):
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
