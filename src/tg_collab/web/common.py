"""Per-request database and template context helpers."""

from __future__ import annotations

import sqlite3

from fastapi import Request

from tg_collab.config import AppPaths, get_language, load_settings
from tg_collab.db import init_db
from tg_collab.reports import build_rows, tab_counts
from tg_collab.similarity import niche_tags
from tg_collab.store import active_profile_id, get_profile, list_profiles


def app_paths(request: Request) -> AppPaths:
    return request.app.state.paths


def connect(request: Request) -> sqlite3.Connection:
    return init_db(app_paths(request).db_path)


def base_context(
    request: Request, conn: sqlite3.Connection, chrome: bool = True, **extra: object
) -> dict:
    settings = load_settings(conn)
    lang = get_language(settings)
    profile = get_profile(conn)
    context: dict = {
        "request": request,
        "lang": lang,
        "settings": settings,
        "profile": profile,
        "profiles": list_profiles(conn),
        "active_profile_id": active_profile_id(conn),
    }
    if chrome:
        if "counts" not in extra:
            rows = build_rows(conn, profile, settings) if profile is not None else []
            context["counts"] = tab_counts(rows)
        if "niche_terms" not in extra:
            terms, tags, domains = niche_tags(profile)
            context["niche_terms"] = terms
            context["niche_hashtags"] = tags
            context["niche_domains"] = domains
    context.update(extra)
    return context
