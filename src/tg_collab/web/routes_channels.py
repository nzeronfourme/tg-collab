"""Channel detail page and CRM actions (status, notes, draft, generate)."""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from tg_collab.config import load_settings
from tg_collab.models import STATUSES
from tg_collab.reports import build_row
from tg_collab.similarity import build_niche_profile
from tg_collab.store import (
    active_profile_id,
    get_channel,
    get_posts,
    get_profile,
    latest_draft,
    save_draft,
    set_notes,
    set_status,
)
from tg_collab.web.common import base_context, connect
from tg_collab.web.generate import run_draft_job
from tg_collab.web.jobs import JobError

router = APIRouter()


def _detail_context(request: Request, conn, channel) -> dict:
    settings = load_settings(conn)
    profile = get_profile(conn)
    draft = latest_draft(conn, channel.id)
    return base_context(
        request,
        conn,
        channel=channel,
        row=build_row(
            conn,
            profile,
            channel,
            settings,
            niche=build_niche_profile(profile) if profile is not None else None,
        ),
        posts=get_posts(conn, channel.id, limit=10),
        draft_text=draft.text if draft is not None else "",
        channel_id=channel.id,
        statuses=STATUSES,
        contact_candidates=channel.meta.get("contact_candidates") or [],
        plugin=channel.meta.get("plugin"),
        tgstat=channel.meta.get("tgstat"),
    )


def _require_channel(conn, channel_id: int):
    channel = get_channel(conn, channel_id=channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
def channel_detail(request: Request, channel_id: int):
    conn = connect(request)
    try:
        channel = _require_channel(conn, channel_id)
        context = _detail_context(request, conn, channel)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="channel.html", context=context
    )


@router.post("/channels/{channel_id}/status", response_class=HTMLResponse)
def update_status(
    request: Request,
    channel_id: int,
    status: str = Form(...),
):
    conn = connect(request)
    try:
        channel = _require_channel(conn, channel_id)
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail="unknown status")
        channel = set_status(conn, channel.username, status)
        context = base_context(request, conn, channel=channel, statuses=STATUSES)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="_status_badge.html", context=context
    )


@router.post("/channels/{channel_id}/draft", response_class=HTMLResponse)
def save_draft_action(
    request: Request,
    channel_id: int,
    text: str = Form(""),
):
    if not text.strip():
        raise HTTPException(status_code=422, detail="draft text is empty")
    conn = connect(request)
    try:
        channel = _require_channel(conn, channel_id)
        save_draft(conn, channel.id, text)
        if channel.status in ("found", "shortlisted"):
            set_status(conn, channel.username, "drafted")
        context = base_context(
            request,
            conn,
            channel=channel,
            draft_text=text,
            channel_id=channel.id,
        )
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="_draft_block.html", context=context
    )


@router.post("/channels/{channel_id}/notes", response_class=HTMLResponse)
def save_notes_action(
    request: Request,
    channel_id: int,
    notes: str = Form(""),
):
    conn = connect(request)
    try:
        channel = _require_channel(conn, channel_id)
        channel = set_notes(conn, channel.username, notes)
        context = base_context(request, conn, channel=channel)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="_notes_block.html", context=context
    )


@router.post("/channels/{channel_id}/generate")
def generate(request: Request, channel_id: int):
    conn = connect(request)
    try:
        _require_channel(conn, channel_id)
        settings = load_settings(conn)
        profile_id = active_profile_id(conn)
    finally:
        conn.close()
    paths = request.app.state.paths
    try:
        job_id = request.app.state.jobs.start(
            "draft",
            partial(run_draft_job, paths, settings, [channel_id], profile_id=profile_id),
            profile_id=profile_id,
        )
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    from tg_collab.web.routes_jobs import job_response

    return job_response(request, job_id, status_code=202)
