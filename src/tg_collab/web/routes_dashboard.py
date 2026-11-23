"""Dashboard: four dynamic tabs, filters, and export."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tg_collab.commands.agent_prompt import build_task
from tg_collab.commands.import_cmd import apply_limit, parse_payload, run_import
from tg_collab.config import get_language, get_setting, load_settings
from tg_collab.db import init_db
from tg_collab.models import CONTACT_STATUSES, STATUSES, utcnow
from tg_collab.niche import (
    DOMAIN_LIMIT,
    HASHTAG_LIMIT,
    TERM_LIMIT,
    Niche,
    load_niche,
    save_niche,
    validate_niche,
)
from tg_collab.reports import (
    ACTIVITY_FILTERS,
    LANG_FILTERS,
    WORK_STATUSES,
    build_rows,
    export_csv,
    export_markdown,
    filter_by_activity,
    filter_by_language,
    source_counts,
    tab_counts,
    target_range,
)
from tg_collab.services import make_http
from tg_collab.similarity import niche_tags, rescore_channels
from tg_collab.store import get_profile, get_profile_by_id
from tg_collab.web.common import base_context, connect
from tg_collab.web.i18n import t

router = APIRouter()
TABS = ("suitable", "work", "growing", "hidden")
SOURCES = (
    "tg_graph",
    "tg_seed",
    "tgstat",
    "plugin_github",
    "agent_search",
    "agent_import",
    "search_brave",
    "search_searxng",
    "search_lyzem",
    "search_mojeek",
    "search_bing",
    "search_yahoo",
    "search_startpage",
    "search_yandex",
    "search_google",
    "search_duckduckgo",
    "search_hn",
)
FLAGS = ("inactive", "suspicious_views", "preview_disabled")


def _select(
    rows: list[dict],
    tab: str,
    status: str | None,
    contact: str | None,
    source: str | None,
    flag: str | None,
    q: str | None,
) -> list[dict]:
    if tab == "work":
        rows = [row for row in rows if row["status"] in WORK_STATUSES]
    elif tab == "hidden":
        rows = [row for row in rows if row["status"] == "ignored"]
    else:
        rows = [row for row in rows if row["tab"] == tab]
    if status:
        rows = [row for row in rows if row["status"] == status]
    if contact:
        rows = [row for row in rows if row["contact_status"] == contact]
    if source:
        rows = [row for row in rows if row["source"] == source]
    if flag:
        rows = [row for row in rows if flag in row["flags"]]
    if q:
        needle = q.lower()
        rows = [
            row
            for row in rows
            if needle in (row["username"] or "").lower()
            or needle in (row["title"] or "").lower()
        ]
    return rows


def _context(
    request: Request,
    active_tab: str,
    status,
    contact,
    source,
    flag,
    q,
    lang,
    activity,
) -> tuple[dict, int]:
    conn = connect(request)
    try:
        settings = load_settings(conn)
        profile = get_profile(conn)
        all_rows = build_rows(
            conn, profile, settings, tab="all", lang="all", activity="all"
        )
        language_rows = filter_by_language(all_rows, lang, profile)
        rows = filter_by_activity(language_rows, activity)
        inactive_hidden = (
            0
            if activity == "all"
            else sum(1 for row in language_rows if "inactive" in row["flags"])
        )
        selected = _select(rows, active_tab, status, contact, source, flag, q)
        context = base_context(
            request,
            conn,
            rows=selected,
            counts=tab_counts(rows),
            target=target_range(profile, settings),
            source_counts=source_counts(rows),
            active_tab=active_tab,
            inactive_hidden=inactive_hidden,
            filters={
                "q": q or "",
                "status": status or "",
                "contact": contact or "",
                "source": source or "",
                "flag": flag or "",
                "lang": lang or "",
                "activity": activity or "",
            },
            statuses=STATUSES,
            contact_statuses=CONTACT_STATUSES,
            sources=SOURCES,
            flag_names=FLAGS,
            channels=len(all_rows),
        )
        context["niche_form"] = {
            "terms": ", ".join(context.get("niche_terms") or []),
            "hashtags": ", ".join(context.get("niche_hashtags") or []),
            "domains": ", ".join(context.get("niche_domains") or []),
        }
        return context, len(rows)
    finally:
        conn.close()


def _lang_filter(lang: str | None) -> str | None:
    return lang if lang in LANG_FILTERS else None


def _activity_filter(activity: str | None) -> str | None:
    return "all" if activity in ACTIVITY_FILTERS else None


def _split_tags(raw: str | None) -> list[str]:
    """Comma- or newline-separated tags without the empty pieces."""
    return [piece.strip() for piece in re.split(r"[,\n]+", raw or "") if piece.strip()]


def _manual_niche(
    terms: list[str],
    hashtags: list[str],
    domains: list[str],
    previous: Niche | None,
) -> Niche | None:
    """A manual niche from the editor; ``None`` when the input is unusable."""
    if not terms or len(terms) > TERM_LIMIT:
        return None
    if len(hashtags) > HASHTAG_LIMIT or len(domains) > DOMAIN_LIMIT:
        return None
    if any(not 2 <= len(term) <= 40 for term in terms):
        return None
    payload = {
        "terms": terms,
        "hashtags": hashtags,
        "domains": domains,
        "stopwords": list(previous.stopwords) if previous is not None else [],
        "summary": previous.summary if previous is not None else "",
        "source": "manual",
        "generated_at": utcnow(),
    }
    return validate_niche(payload, min_terms=1, default_source="manual")


def _niche_error_page(request: Request, submitted: dict[str, str]) -> HTMLResponse:
    context, _ = _context(
        request,
        "suitable",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    context["niche_error"] = t("niche_edit_invalid", context["lang"])
    context["niche_form"] = submitted
    context["job"] = request.app.state.jobs.running()
    return request.app.state.templates.TemplateResponse(
        request=request, name="index.html", context=context
    )


@router.post("/profiles/niche", response_class=HTMLResponse)
async def save_profile_niche(request: Request):
    """Owner's manual niche tags: validate, store, and rescore the profile."""
    form = await request.form()
    terms = _split_tags(str(form.get("terms") or ""))
    hashtags = [
        piece.lstrip("#").strip()
        for piece in _split_tags(str(form.get("hashtags") or ""))
    ]
    domains = _split_tags(str(form.get("domains") or ""))
    submitted = {
        "terms": ", ".join(terms),
        "hashtags": ", ".join(hashtags),
        "domains": ", ".join(domains),
    }
    conn = connect(request)
    try:
        profile = get_profile(conn)
        if profile is None:
            return _niche_error_page(request, submitted)
        niche = _manual_niche(terms, hashtags, domains, load_niche(profile))
        if niche is None:
            return _niche_error_page(request, submitted)
        save_niche(conn, niche, profile_id=profile.id)
        refreshed = get_profile_by_id(conn, profile.id)
        if refreshed is not None:
            rescore_channels(
                conn, refreshed, load_settings(conn), profile_id=profile.id
            )
    finally:
        conn.close()
    return RedirectResponse(url="/", status_code=303)


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tab: str = "suitable",
    status: str | None = None,
    contact: str | None = None,
    source: str | None = None,
    flag: str | None = None,
    q: str | None = None,
    lang: str | None = None,
    activity: str | None = None,
):
    active = tab if tab in TABS else "suitable"
    context, _ = _context(
        request,
        active,
        status,
        contact,
        source,
        flag,
        q,
        _lang_filter(lang),
        _activity_filter(activity),
    )
    context["job"] = request.app.state.jobs.running()
    return request.app.state.templates.TemplateResponse(
        request=request, name="index.html", context=context
    )


@router.get("/channels", response_class=HTMLResponse)
def channels_partial(
    request: Request,
    tab: str = "suitable",
    status: str | None = None,
    contact: str | None = None,
    source: str | None = None,
    flag: str | None = None,
    q: str | None = None,
    lang: str | None = None,
    activity: str | None = None,
):
    """HTML fragment for the filter form; full dashboard on a plain reload.

    HTMX pushes this URL, so a browser reload or a back/forward navigation
    lands here without ``HX-Request`` and gets the whole page instead of a
    bare fragment.
    """
    active = tab if tab in TABS else "suitable"
    context, _ = _context(
        request,
        active,
        status,
        contact,
        source,
        flag,
        q,
        _lang_filter(lang),
        _activity_filter(activity),
    )
    if request.headers.get("hx-request") != "true":
        context["job"] = request.app.state.jobs.running()
        return request.app.state.templates.TemplateResponse(
            request=request, name="index.html", context=context
        )
    return request.app.state.templates.TemplateResponse(
        request=request, name="_channels_response.html", context=context
    )


@router.get("/agent-prompt", response_class=HTMLResponse)
def agent_prompt_partial(request: Request):
    conn = connect(request)
    try:
        settings = load_settings(conn)
        profile = get_profile(conn)
        prompt = ""
        if profile is not None:
            target = target_range(profile, settings)
            if target is not None:
                terms, tags, domains = niche_tags(profile)
                prompt = build_task(
                    profile,
                    target,
                    terms,
                    get_language(settings),
                    hashtags=tags,
                    domains=domains,
                    region=get_setting(settings, "search_region"),
                )
        context = base_context(request, conn, prompt=prompt)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="_agent_prompt.html", context=context
    )


def _run_import_worker(paths, raw: str) -> dict:
    conn = init_db(paths.db_path)
    try:
        settings = load_settings(conn)
        profile = get_profile(conn)
        if profile is None:
            raise ValueError("no profile yet")
        plan = apply_limit(parse_payload(raw), settings)
        http = make_http(settings)
        try:
            return run_import(conn, settings, http, profile, plan)
        finally:
            http.close()
    finally:
        conn.close()


@router.post("/import", response_class=HTMLResponse)
async def import_from_agent(request: Request):
    form = await request.form()
    raw = str(form.get("targets") or "").strip()
    tab = str(form.get("tab") or "suitable")
    try:
        result = await run_in_threadpool(
            _run_import_worker, request.app.state.paths, raw
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    conn = connect(request)
    try:
        context = base_context(
            request,
            conn,
            chrome=False,
            result=result,
            tab=tab if tab in TABS else "suitable",
        )
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="_import_result.html", context=context
    )


@router.get("/export")
def export(
    request: Request,
    format: str = "md",
    lang: str | None = None,
    activity: str | None = None,
) -> Response:
    chosen = format if format in ("md", "csv") else "md"
    conn = connect(request)
    try:
        settings = load_settings(conn)
        rows = build_rows(
            conn,
            get_profile(conn),
            settings,
            tab="all",
            lang=_lang_filter(lang),
            activity=_activity_filter(activity),
        )
        rows = [row for row in rows if row["status"] != "ignored"]
        content = (
            export_markdown(rows, get_language(settings))
            if chosen == "md"
            else export_csv(rows)
        )
    finally:
        conn.close()
    media = "text/markdown; charset=utf-8" if chosen == "md" else "text/csv; charset=utf-8"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="outreach.{chosen}"'},
    )
