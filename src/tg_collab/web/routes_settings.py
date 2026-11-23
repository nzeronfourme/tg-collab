"""Settings page: masked secrets, validation, runtime source of truth."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tg_collab.config import (
    get_language,
    load_settings,
    normalize_proxy,
    proxy_is_valid,
    searxng_is_valid,
    set_settings,
)
from tg_collab.services import check_telegram
from tg_collab.store import (
    create_profile,
    delete_profile,
    get_profile,
    rename_profile,
    set_active_profile,
    set_profile_seed_channels,
)
from tg_collab.web.common import base_context, connect
from tg_collab.web.i18n import t
from tg_collab.web.jobs import start_niche_job

router = APIRouter()
BOOL_KEYS = (
    "sources_tg_public",
    "sources_tgstat",
    "sources_plugin_github",
    "sources_search_lyzem",
    "sources_search_bing",
    "sources_search_yahoo",
    "sources_search_startpage",
    "sources_search_hn",
    "sources_agent_search",
)
SECRET_KEYS = ("tgstat_token", "github_token", "proxy_url", "brave_search_api_key")
TEXT_KEYS = (
    "mine_username",
    "search_engines",
    "search_region",
    "search_max_queries",
    "search_max_results",
    "search_cache_days",
    "search_delay_ms",
    "find_max_candidates",
    "agent_import_max",
    "agent_search_max",
    "agent_search_timeout_sec",
    "agent_search_concurrency",
    "agent_commands",
    "github_max_repos_unauth",
    "tgstat_country",
    "tgstat_categories",
    "tgstat_queries",
    "github_queries",
    "agent_command",
    "min_mult",
    "max_mult",
    "agent_timeout_sec",
    "ui_port",
    "refresh_days",
    "profile_post_limit",
    "http_delay_ms",
    "http_timeout_sec",
    "http_retries",
    "find_max_depth",
    "find_max_channels",
    "seed_channels",
    "github_max_repos",
    "flag_inactive_days",
    "flag_cv_threshold",
    "flag_ratio_threshold",
    "flag_err_threshold",
)
INT_KEYS = (
    "agent_timeout_sec",
    "ui_port",
    "refresh_days",
    "profile_post_limit",
    "http_delay_ms",
    "http_timeout_sec",
    "http_retries",
    "github_max_repos",
    "github_max_repos_unauth",
    "search_max_queries",
    "search_max_results",
    "search_cache_days",
    "search_delay_ms",
    "find_max_candidates",
    "agent_import_max",
    "agent_search_max",
    "agent_search_timeout_sec",
    "agent_search_concurrency",
    "flag_inactive_days",
)
ZERO_ALLOWED = ("http_delay_ms", "refresh_days", "http_retries", "search_delay_ms", "search_cache_days")
FLOAT_KEYS = ("flag_cv_threshold", "flag_ratio_threshold", "flag_err_threshold")


def _size_hint(values: dict[str, str], profile, lang: str) -> str:
    """Computed partner range for the stored multipliers and the profile size."""
    subscribers = getattr(profile, "subscribers", None)
    if not subscribers:
        return ""
    try:
        min_mult = float(values.get("min_mult") or "0.3")
        max_mult = float(values.get("max_mult") or "3.0")
    except (TypeError, ValueError):
        return ""
    if not (0 < min_mult < max_mult):
        return ""
    low = round(subscribers * min_mult)
    high = round(subscribers * max_mult)
    group = "\u00a0" if lang == "ru" else ","
    return t("size_hint", lang).format(
        min_mult=f"{min_mult:g}",
        max_mult=f"{max_mult:g}",
        subscribers=f"{subscribers:,}".replace(",", group),
        low=f"{low:,}".replace(",", group),
        high=f"{high:,}".replace(",", group),
    )


def _profile_values(conn, values: dict[str, str]) -> dict[str, str]:
    """The active profile's username and seeds replace the legacy globals on the page."""
    profile = get_profile(conn)
    if profile is None:
        return values
    scoped = dict(values)
    scoped["mine_username"] = profile.username
    scoped["seed_channels"] = profile.seed_channels or ""
    return scoped


def _context(
    request: Request,
    conn,
    values: dict[str, str],
    errors: dict[str, str],
    saved: bool,
    profiles_error: str = "",
):
    lang = get_language(values)
    return base_context(
        request,
        conn,
        values=values,
        errors=errors,
        saved=saved,
        profiles_error=profiles_error,
        size_hint=_size_hint(values, get_profile(conn), lang),
    )


def _render(request: Request, conn, errors: dict[str, str], saved: bool):
    values = _profile_values(conn, load_settings(conn))
    context = _context(request, conn, values, errors, saved)
    return request.app.state.templates.TemplateResponse(
        request=request, name="settings.html", context=context
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    conn = connect(request)
    try:
        return _render(request, conn, {}, False)
    finally:
        conn.close()


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request):
    form = await request.form()
    updates: dict[str, str] = {}
    errors: dict[str, str] = {}

    for key in BOOL_KEYS:
        updates[key] = "1" if form.get(key) else "0"
    # A control removed from the page is absent from the form: leave the stored
    # value alone instead of blanking it. An explicitly posted key still saves.
    for key in TEXT_KEYS:
        if key in form:
            updates[key] = str(form.get(key) or "").strip()
    for key in SECRET_KEYS:
        if key == "proxy_url":
            continue
        value = str(form.get(key) or "").strip()
        if value:
            updates[key] = value

    clear_proxy = bool(form.get("clear_proxy"))
    proxy_raw = str(form.get("proxy_url") or "").strip()
    if clear_proxy:
        updates["proxy_url"] = ""
    elif proxy_raw:
        normalized = normalize_proxy(proxy_raw)
        if not proxy_is_valid(normalized):
            errors["proxy_url"] = "expected http(s)://host:port or socks5://host:port"
        else:
            updates["proxy_url"] = normalized

    searxng_raw = str(form.get("searxng_url") or "").strip()
    if not searxng_raw:
        updates["searxng_url"] = ""
    elif not searxng_is_valid(searxng_raw):
        errors["searxng_url"] = "expected http(s)://host without credentials"
    else:
        updates["searxng_url"] = searxng_raw

    language = str(form.get("language") or "en")
    if language not in ("en", "ru"):
        errors["language"] = "language must be en or ru"
        language = "en"
    updates["language"] = language

    min_raw = updates.get("min_mult")
    max_raw = updates.get("max_mult")
    if min_raw is not None and max_raw is not None:
        try:
            min_mult = float(min_raw)
            max_mult = float(max_raw)
        except ValueError:
            errors["corridor"] = "multipliers must be numbers"
        else:
            if not (0 < min_mult < max_mult):
                errors["corridor"] = "expected 0 < min_mult < max_mult"
            else:
                updates["min_mult"] = str(min_mult)
                updates["max_mult"] = str(max_mult)
    elif min_raw is not None or max_raw is not None:
        updates.pop("min_mult", None)
        updates.pop("max_mult", None)

    for key in INT_KEYS:
        if key not in updates:
            continue
        value = updates[key]
        if not value:
            updates.pop(key, None)
            continue
        if not value.isdigit():
            errors[key] = "expected a number"
        elif key not in ZERO_ALLOWED and int(value) <= 0:
            errors[key] = "expected a positive integer"

    for key in FLOAT_KEYS:
        if key not in updates:
            continue
        value = updates[key]
        if not value:
            updates.pop(key, None)
            continue
        try:
            number = float(value)
        except ValueError:
            errors[key] = "expected a number"
        else:
            if number <= 0:
                errors[key] = "expected a positive number"
            else:
                updates[key] = str(number)

    conn = connect(request)
    try:
        profile = get_profile(conn)
        if not errors and profile is not None and "mine_username" in updates:
            try:
                rename_profile(conn, updates["mine_username"], profile_id=profile.id)
            except ValueError as exc:
                key = (
                    "profiles_rename_duplicate"
                    if "already uses" in str(exc)
                    else "profiles_username_required"
                )
                errors["mine_username"] = t(key, language)
        if not errors and profile is not None and "seed_channels" in updates:
            set_profile_seed_channels(
                conn, updates["seed_channels"], profile_id=profile.id
            )
        if errors:
            merged = _profile_values(conn, load_settings(conn))
            for key, value in updates.items():
                if key not in SECRET_KEYS or key == "proxy_url":
                    merged[key] = value
            context = _context(request, conn, merged, errors, False)
            return request.app.state.templates.TemplateResponse(
                request=request, name="settings.html", context=context
            )
        set_settings(conn, updates)
        return _render(request, conn, {}, True)
    finally:
        conn.close()


@router.post("/settings/test-network", response_class=HTMLResponse)
async def test_network(request: Request):
    form = await request.form()
    submitted = str(form.get("proxy_url") or "").strip()
    conn = connect(request)
    try:
        settings = load_settings(conn)
        proxy = submitted if submitted else settings.get("proxy_url", "")
        result = check_telegram(settings, proxy_url=proxy)
        context = base_context(request, conn, result=result)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="_network_test.html", context=context
    )


def _profiles_page(request: Request, conn, key: str):
    values = _profile_values(conn, load_settings(conn))
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=_context(
            request, conn, values, {}, False, profiles_error=t(key, get_language(values))
        ),
    )


def _next_target(form) -> str:
    target = str(form.get("next") or "/")
    return target if target in ("/", "/settings") else "/"


def _profile_id(form) -> int | None:
    raw = str(form.get("profile_id") or "").strip()
    return int(raw) if raw.isdigit() else None


@router.post("/profiles/use")
async def use_profile(request: Request):
    form = await request.form()
    profile_id = _profile_id(form)
    conn = connect(request)
    try:
        if profile_id is None:
            return _profiles_page(request, conn, "profiles_unknown")
        try:
            profile = set_active_profile(conn, profile_id)
        except LookupError:
            return _profiles_page(request, conn, "profiles_unknown")
        settings = load_settings(conn)
    finally:
        conn.close()
    start_niche_job(request.app.state.jobs, request.app.state.paths, settings, profile)
    return RedirectResponse(url=_next_target(form), status_code=303)


@router.post("/profiles/add")
async def add_profile(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    conn = connect(request)
    try:
        try:
            created = create_profile(conn, username)
        except ValueError as exc:
            key = "profiles_duplicate" if "already exists" in str(exc) else "profiles_username_required"
            return _profiles_page(request, conn, key)
        set_active_profile(conn, int(created.id or 0))
        settings = load_settings(conn)
    finally:
        conn.close()
    start_niche_job(request.app.state.jobs, request.app.state.paths, settings, created)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/profiles/remove")
async def remove_profile(request: Request):
    form = await request.form()
    profile_id = _profile_id(form)
    conn = connect(request)
    try:
        if profile_id is None:
            return _profiles_page(request, conn, "profiles_unknown")
        try:
            delete_profile(conn, profile_id)
        except LookupError:
            return _profiles_page(request, conn, "profiles_unknown")
        except ValueError:
            return _profiles_page(request, conn, "profiles_last_error")
    finally:
        conn.close()
    return RedirectResponse(url=_next_target(form), status_code=303)
