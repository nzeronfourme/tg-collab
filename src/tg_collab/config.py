"""Data directory resolution and typed runtime settings."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR_ENV = "TGCOLLAB_DATA_DIR"
DEFAULT_HOME_NAME = ".tg-collab"
ENV_PREFIX = "TGCOLLAB_"

#: Zero-key HTML search engines on by default.
DEFAULT_SEARCH_ENGINES = (
    "yandex",
    "google",
    "duckduckgo",
    "mojeek",
    "bing",
    "yahoo",
    "startpage",
)
#: The list shipped before Mojeek; a stored copy of it means "defaults".
LEGACY_SEARCH_ENGINES = ("yandex", "google", "duckduckgo")
#: The list shipped before Bing/Yahoo/Startpage; it also means "defaults".
LEGACY_SEARCH_ENGINES_V4 = ("yandex", "google", "duckduckgo", "mojeek")

#: Every known setting with its default value. The settings table stores strings.
SETTING_DEFAULTS: dict[str, str] = {
    "language": "en",
    "mine_username": "",
    "active_profile_id": "",
    "min_mult": "0.3",
    "max_mult": "3.0",
    "refresh_days": "7",
    "profile_post_limit": "30",
    "find_max_depth": "",
    "find_max_channels": "",
    "seed_channels": "",
    "http_delay_ms": "1000",
    "http_timeout_sec": "20",
    "http_retries": "2",
    "proxy_url": "",
    "flag_inactive_days": "14",
    "flag_cv_threshold": "0.8",
    "flag_ratio_threshold": "5.0",
    "flag_err_threshold": "0.5",
    "search_engines": ",".join(DEFAULT_SEARCH_ENGINES),
    "search_region": "ru",
    "search_max_queries": "10",
    "search_max_results": "20",
    "search_cache_days": "7",
    "search_delay_ms": "2500",
    "brave_search_api_key": "",
    "searxng_url": "",
    "find_max_candidates": "500",
    "agent_import_max": "50",
    "agent_search_max": "30",
    "agent_search_timeout_sec": "600",
    "agent_search_concurrency": "2",
    "agent_commands": "",
    "sources_tg_public": "1",
    "sources_tgstat": "1",
    "sources_plugin_github": "1",
    "sources_search_lyzem": "0",
    "sources_search_bing": "1",
    "sources_search_yahoo": "1",
    "sources_search_startpage": "1",
    "sources_search_hn": "1",
    "sources_agent_search": "1",
    "tgstat_token": "",
    "tgstat_country": "",
    "tgstat_categories": "",
    "tgstat_queries": "",
    "github_token": "",
    "github_queries": "",
    "github_max_repos": "20",
    "github_max_repos_unauth": "10",
    "agent_command": "",
    "agent_timeout_sec": "300",
    "ui_port": "8080",
}

#: Third-party conventions accepted as overrides for well-known keys.
ENV_ALIASES: dict[str, str] = {
    "TGSTAT_TOKEN": "tgstat_token",
    "GITHUB_TOKEN": "github_token",
    "BRAVE_SEARCH_API_KEY": "brave_search_api_key",
    "SEARXNG_URL": "searxng_url",
}

SECRET_KEYS = frozenset({"tgstat_token", "github_token", "proxy_url", "brave_search_api_key"})
LANGUAGES = ("en", "ru")
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
SEARXNG_SCHEMES = ("http", "https")
#: The old default; a stored copy is upgraded to the current default on load.
LEGACY_AGENT_SEARCH_TIMEOUT = "240"


@dataclass(frozen=True)
class AppPaths:
    """Resolved filesystem locations for one tg-collab installation."""

    home: Path
    db_path: Path


def resolve_paths(data_dir: str | None = None) -> AppPaths:
    """Resolve the data directory: explicit argument, env var, then ``./.tg-collab``."""
    raw = data_dir or os.environ.get(DATA_DIR_ENV) or str(Path.cwd() / DEFAULT_HOME_NAME)
    home = Path(raw).expanduser().resolve()
    return AppPaths(home=home, db_path=home / "data.db")


def ensure_home(paths: AppPaths) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)


def normalize_proxy(value: str | None) -> str:
    """Accept ``host:port`` as well as full proxy URLs; prefix a missing scheme."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def proxy_env(settings: Mapping[str, str]) -> dict[str, str]:
    """Proxy variables handed to a local agent subprocess (empty when no proxy)."""
    proxy = normalize_proxy(get_setting(settings, "proxy_url"))
    if not proxy:
        return {}
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy}


def proxy_is_valid(value: str | None) -> bool:
    raw = normalize_proxy(value)
    if not raw:
        return True
    parts = urlsplit(raw)
    if parts.scheme not in PROXY_SCHEMES or not parts.hostname:
        return False
    try:
        return parts.port is not None
    except ValueError:
        return False


def searxng_is_valid(value: str | None) -> bool:
    """One or more SearXNG base URLs: http(s), a hostname, and no credentials."""
    parts_raw = [part.strip() for part in re.split(r"[,\n]+", value or "")]
    parts_raw = [part for part in parts_raw if part]
    if not parts_raw:
        return True
    for raw in parts_raw:
        try:
            parts = urlsplit(raw)
            hostname = parts.hostname
        except ValueError:
            return False
        if parts.scheme not in SEARXNG_SCHEMES or not hostname:
            return False
        if parts.username is not None or parts.password is not None:
            return False
    return True


def resolve_search_engines(value: str | None) -> list[str]:
    """Split a stored engine list; the legacy defaults gain the new engines."""
    names: list[str] = []
    for raw in re.split(r"[,;\s]+", value or ""):
        name = raw.strip().lower()
        if name and name not in names:
            names.append(name)
    if not names or tuple(names) in {LEGACY_SEARCH_ENGINES, LEGACY_SEARCH_ENGINES_V4}:
        return list(DEFAULT_SEARCH_ENGINES)
    return names


def _env_overrides() -> dict[str, str]:
    overrides: dict[str, str] = {}
    for key in SETTING_DEFAULTS:
        env_name = ENV_PREFIX + key.upper()
        if env_name in os.environ:
            overrides[key] = os.environ[env_name]
    for env_name, key in ENV_ALIASES.items():
        if env_name in os.environ:
            overrides[key] = os.environ[env_name]
    return overrides


def load_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Return defaults, then stored values, then environment overrides.

    The environment wins over the database and is never written back.
    """
    settings = dict(SETTING_DEFAULTS)
    for row in conn.execute("SELECT key, value FROM settings"):
        key = row["key"] if isinstance(row, sqlite3.Row) else row[0]
        value = row["value"] if isinstance(row, sqlite3.Row) else row[1]
        if key in SETTING_DEFAULTS:
            settings[key] = value
    if settings.get("agent_search_timeout_sec") == LEGACY_AGENT_SEARCH_TIMEOUT:
        settings["agent_search_timeout_sec"] = SETTING_DEFAULTS["agent_search_timeout_sec"]
    settings.update(_env_overrides())
    return settings


def get_setting(settings: Mapping[str, str], key: str, default: str = "") -> str:
    if key in settings and settings[key] is not None:
        return str(settings[key])
    return SETTING_DEFAULTS.get(key, default)


def _raw(settings: Mapping[str, str], key: str) -> str:
    return get_setting(settings, key).strip()


def get_int(settings: Mapping[str, str], key: str) -> int | None:
    raw = _raw(settings, key)
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_float(settings: Mapping[str, str], key: str) -> float | None:
    raw = _raw(settings, key)
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def get_bool(settings: Mapping[str, str], key: str) -> bool:
    return _raw(settings, key).lower() in {"1", "true", "yes", "on"}


def get_language(settings: Mapping[str, str]) -> str:
    value = _raw(settings, "language").lower()
    return value if value in LANGUAGES else "en"


def set_settings(conn: sqlite3.Connection, updates: Mapping[str, str]) -> None:
    """Persist settings; rejects unknown keys before writing anything."""
    unknown = [key for key in updates if key not in SETTING_DEFAULTS]
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    now = _utcnow()
    with conn:
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), now),
            )


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
