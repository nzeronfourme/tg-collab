"""DB-backed background jobs with progress, cancellation, and isolated connections."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from functools import partial
from pathlib import Path
from typing import Any, Callable

from tg_collab.config import AppPaths, get_bool
from tg_collab.db import init_db
from tg_collab.discovery import SOURCE_KEYS, DiscoveryRun
from tg_collab.enrichment import enrich_channel
from tg_collab.models import Job, Profile
from tg_collab.niche import ensure_niche, generate_niche, load_niche, save_niche
from tg_collab.services import init_profile, make_http
from tg_collab.similarity import rescore_channels
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.registry import build_discovery_providers
from tg_collab.sources.tg_public import TelegramPublicClient
from tg_collab.store import (
    active_profile_id,
    create_job,
    get_job,
    get_profile,
    get_profile_by_id,
    latest_running_job,
    update_job,
)

JobFn = Callable[..., Any]

#: Progress windows for the phases of a `find` run, in percent. Every phase
#: starts at its low bound and never crosses the next phase's low bound.
FIND_PHASES: dict[str, tuple[int, int]] = {
    "start": (0, 5),
    "niche": (5, 10),
    "sources": (10, 25),
    "enrich": (25, 95),
    "finalize": (95, 100),
}


def find_progress(phase: str, done: int = 0, total: int = 0) -> int:
    """Overall percent for a `find` phase; enrichment is interpolated."""
    low, high = FIND_PHASES[phase]
    if phase != "enrich":
        return low
    ratio = min(max(done, 0) / max(total, 1), 1.0)
    return min(high, low + int((high - low) * ratio))


class JobError(RuntimeError):
    """A job cannot be started (for example, the same kind is already running)."""


class JobManager:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._cancel: dict[str, threading.Event] = {}
        self._done: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def start(
        self, kind: str, fn: JobFn, *args: Any, profile_id: int | None = None
    ) -> str:
        with self._lock:
            running = self.running(kind)
            if running is not None:
                raise JobError(f"a {kind} job is already running: {running.id}")
            conn = init_db(self.db_path)
            try:
                resolved = profile_id if profile_id is not None else active_profile_id(conn)
                job = create_job(conn, kind, profile_id=resolved)
            finally:
                conn.close()
            cancel = threading.Event()
            done = threading.Event()
            self._cancel[job.id] = cancel
            self._done[job.id] = done
            thread = threading.Thread(
                target=self._run,
                args=(job.id, fn, args, cancel, done),
                daemon=True,
                name=f"tg-collab-{kind}-{job.id[:8]}",
            )
            self._threads[job.id] = thread
            thread.start()
            return job.id

    def wait(self, job_id: str, timeout: float = 10.0) -> Job | None:
        done = self._done.get(job_id)
        if done is not None:
            done.wait(timeout)
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(0.1)
        return self.get(job_id)

    def cancel(self, job_id: str) -> None:
        event = self._cancel.get(job_id)
        if event is not None:
            event.set()

    def get(self, job_id: str) -> Job | None:
        conn = init_db(self.db_path)
        try:
            return get_job(conn, job_id)
        finally:
            conn.close()

    def running(self, kind: str | None = None) -> Job | None:
        conn = init_db(self.db_path)
        try:
            return latest_running_job(conn, kind, profile_id=active_profile_id(conn))
        finally:
            conn.close()

    def shutdown(self) -> None:
        with self._lock:
            events = list(self._cancel.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for thread in threads:
            thread.join(2.0)

    def alive_threads(self) -> int:
        return sum(1 for thread in list(self._threads.values()) if thread.is_alive())

    # -- internals ---------------------------------------------------------

    def _write(self, conn: sqlite3.Connection, job_id: str, **fields: Any) -> None:
        for attempt in range(2):
            try:
                update_job(conn, job_id, **fields)
                return
            except sqlite3.OperationalError as exc:
                if attempt or "locked" not in str(exc).lower():
                    raise
                time.sleep(0.05)

    def _run(
        self,
        job_id: str,
        fn: JobFn,
        args: tuple[Any, ...],
        cancel: threading.Event,
        done: threading.Event,
    ) -> None:
        conn = init_db(self.db_path)
        try:

            def progress(value: int, message: str = "") -> None:
                self._write(
                    conn,
                    job_id,
                    progress=max(0, min(int(value), 100)),
                    message=str(message),
                )

            result = fn(progress, cancel, *args)
            payload = json.dumps(result or {}, ensure_ascii=False, default=str)
            if cancel.is_set():
                self._write(conn, job_id, status="cancelled", message="cancelled")
            elif isinstance(result, dict) and result.get("sources_ok") == 0:
                self._write(
                    conn,
                    job_id,
                    status="error",
                    message="every enabled source failed; see the source status below",
                    result_json=payload,
                )
            else:
                self._write(
                    conn,
                    job_id,
                    status="done",
                    progress=100,
                    result_json=payload,
                )
        except Exception as exc:  # noqa: BLE001 - job boundary
            if cancel.is_set():
                self._write(conn, job_id, status="cancelled", message="cancelled")
            else:
                self._write(
                    conn,
                    job_id,
                    status="error",
                    message=f"{type(exc).__name__}: {exc}",
                    log=traceback.format_exc(limit=6),
                )
        finally:
            conn.close()
            done.set()


def run_find_job(
    paths: AppPaths,
    settings: dict[str, str],
    progress: Callable[[int, str], None],
    cancel: threading.Event,
    profile_id: int | None = None,
) -> dict[str, Any]:
    conn = init_db(paths.db_path)
    http = make_http(settings)
    try:
        progress(find_progress("start"), "starting the search")
        profile = (
            get_profile_by_id(conn, profile_id) if profile_id is not None else get_profile(conn)
        )
        if profile is None:
            raise RuntimeError("no profile yet - run `tg-collab init @your_channel` first")
        pinned_id = profile_id if profile_id is not None else profile.id
        niche_warnings: list[str] = []
        if not (profile.niche_json or "").strip():
            progress(find_progress("niche"), "asking the local agent for the niche profile")
            profile, niche_warnings = ensure_niche(
                conn, settings, profile, cancel=cancel, profile_id=pinned_id
            )
            progress(
                find_progress("niche"),
                "niche profile ready" if load_niche(profile) else "using the deterministic niche terms",
            )
        provider_ctx = ProviderContext(
            settings=settings,
            http=http,
            profile=profile,
            store=conn,
            profile_id=pinned_id,
            cancel=cancel,
        )
        provider_ctx.warnings.extend(niche_warnings)
        providers = build_discovery_providers(settings, http, provider_ctx.warnings)
        run = DiscoveryRun(
            settings=settings,
            providers=providers,
            public_client=TelegramPublicClient(http),
            enrich=partial(enrich_channel, ctx=provider_ctx),
            store=conn,
            ctx=provider_ctx,
        )

        def on_progress(done: int, total: int, message: str) -> None:
            progress(
                find_progress("enrich", done, total),
                f"checking channels {done}/{total} - {message}",
            )

        progress(find_progress("sources"), "collecting candidates from the enabled sources")
        stats = run.run(profile.username, progress=on_progress, cancel=cancel)
        progress(find_progress("finalize"), "finalizing the result")
        return {
            "added": stats.added,
            "updated": stats.updated,
            "skipped_fresh": stats.skipped_fresh,
            "seeds": stats.seeds,
            "errors": stats.errors,
            "warnings": stats.warnings,
            "providers": {key: stats.providers.get(key, 0) for key in SOURCE_KEYS},
            "sources_ok": stats.sources_ok,
            "source_status": stats.source_status,
            "source_remedy": stats.source_remedy,
        }
    finally:
        http.close()
        conn.close()


def run_niche_job(
    paths: AppPaths,
    settings: dict[str, str],
    progress: Callable[[int, str], None],
    cancel: threading.Event,
    profile_id: int | None = None,
) -> dict[str, Any]:
    conn = init_db(paths.db_path)
    try:
        profile = (
            get_profile_by_id(conn, profile_id) if profile_id is not None else get_profile(conn)
        )
        if profile is None:
            raise RuntimeError("no profile yet - run `tg-collab init @your_channel` first")
        if (profile.niche_json or "").strip():
            stored = load_niche(profile)
            return {
                "terms": len(stored.terms) if stored is not None else 0,
                "source": stored.source if stored is not None else "stored",
            }
        progress(10, "asking the local agent for the niche profile")
        generated, warning = generate_niche(settings, profile, cancel=cancel)
        if generated is None:
            return {
                "terms": 0,
                "source": "deterministic",
                "warning": warning or "niche: the agent returned no usable terms",
            }
        save_niche(conn, generated, profile_id=profile.id)
        refreshed = get_profile_by_id(conn, profile.id) or profile
        rescore_channels(conn, refreshed, settings, profile_id=profile.id)
        progress(100, "niche profile ready")
        return {"terms": len(generated.terms), "source": generated.source}
    finally:
        conn.close()


def start_niche_job(
    jobs: JobManager,
    paths: AppPaths,
    settings: dict[str, str],
    profile: Profile | None,
) -> None:
    """Ask the agent for tags when the profile has none; a running job is not an error."""
    if profile is None or (profile.niche_json or "").strip():
        return
    if not get_bool(settings, "sources_agent_search"):
        return
    try:
        jobs.start(
            "niche",
            partial(run_niche_job, paths, settings, profile_id=profile.id),
            profile_id=profile.id,
        )
    except JobError:
        pass


def run_init_job(
    paths: AppPaths,
    settings: dict[str, str],
    progress: Callable[[int, str], None],
    cancel: threading.Event,
    profile_id: int | None = None,
) -> dict[str, Any]:
    conn = init_db(paths.db_path)
    http = make_http(settings)
    try:
        pinned = get_profile_by_id(conn, profile_id) if profile_id is not None else get_profile(conn)
        username = (pinned.username if pinned is not None else "").strip() or (
            settings.get("mine_username") or ""
        ).strip()
        if not username:
            raise RuntimeError(
                "set your channel username in Settings first (mine_username)"
            )
        progress(10, f"profiling @{username}")
        result = init_profile(conn, settings, http, username, profile_id=profile_id)
        profile = result["profile"]
        niche_warnings: list[str] = []
        if not (profile.niche_json or "").strip() and get_bool(
            settings, "sources_agent_search"
        ):
            progress(90, "asking the local agent for the niche profile")
            profile, niche_warnings = ensure_niche(
                conn, settings, profile, cancel=cancel, profile_id=profile.id
            )
        progress(100, "profile updated")
        payload: dict[str, Any] = {
            "username": profile.username,
            "subscribers": profile.subscribers,
            "avg_views": profile.avg_views,
            "range": {
                "low": result["range"].low,
                "high": result["range"].high,
            },
        }
        if niche_warnings:
            payload["warning"] = "; ".join(niche_warnings)
        return payload
    finally:
        http.close()
        conn.close()
