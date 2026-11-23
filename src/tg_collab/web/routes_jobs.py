"""Background job endpoints for the dashboard."""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from tg_collab.config import load_settings
from tg_collab.store import active_profile_id
from tg_collab.web.common import base_context, connect
from tg_collab.web.generate import run_draft_job
from tg_collab.web.i18n import t
from tg_collab.web.jobs import JobError, run_find_job, run_init_job

router = APIRouter()


def job_response(
    request: Request, job_id: str, status_code: int = 200, notice: bool = False
) -> HTMLResponse:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    conn = connect(request)
    try:
        context = base_context(request, conn, chrome=False, job=job)
    finally:
        conn.close()
    if notice:
        context["notice"] = t("job_already_running", context["lang"])
    response = request.app.state.templates.TemplateResponse(
        request=request, name="_job.html", context=context, status_code=status_code
    )
    if job.status != "running":
        response.headers["HX-Refresh"] = "true"
    return response


def _settings_and_profile(request: Request) -> tuple[dict[str, str], int | None]:
    conn = connect(request)
    try:
        return load_settings(conn), active_profile_id(conn)
    finally:
        conn.close()


def _start(
    request: Request,
    kind: str,
    fn,
    profile_id: int | None = None,
    status_code: int = 202,
) -> HTMLResponse:
    try:
        job_id = request.app.state.jobs.start(kind, fn, profile_id=profile_id)
    except JobError:
        running = request.app.state.jobs.running(kind)
        if running is None:
            raise HTTPException(status_code=409, detail="a run is already in progress") from None
        return job_response(request, running.id, notice=True)
    return job_response(request, job_id, status_code=status_code)


@router.post("/jobs/find")
def start_find(request: Request) -> HTMLResponse:
    paths = request.app.state.paths
    settings, profile_id = _settings_and_profile(request)
    return _start(
        request,
        "find",
        partial(run_find_job, paths, settings, profile_id=profile_id),
        profile_id=profile_id,
    )


@router.post("/jobs/init")
def start_init(request: Request) -> HTMLResponse:
    paths = request.app.state.paths
    settings, profile_id = _settings_and_profile(request)
    return _start(
        request,
        "init",
        partial(run_init_job, paths, settings, profile_id=profile_id),
        profile_id=profile_id,
    )


@router.post("/jobs/draft")
async def start_draft(request: Request) -> HTMLResponse:
    form = await request.form()
    ids = [int(value) for value in form.getlist("ids") if str(value).strip().isdigit()]
    if not ids:
        raise HTTPException(status_code=400, detail="select at least one channel")
    paths = request.app.state.paths
    settings, profile_id = _settings_and_profile(request)
    return _start(
        request,
        "draft",
        partial(run_draft_job, paths, settings, ids, profile_id=profile_id),
        profile_id=profile_id,
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str) -> HTMLResponse:
    request.app.state.jobs.cancel(job_id)
    return job_response(request, job_id, status_code=202)


@router.get("/jobs/{job_id}")
def job_status(request: Request, job_id: str) -> HTMLResponse:
    return job_response(request, job_id)
