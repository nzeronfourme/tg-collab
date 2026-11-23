"""Draft generation through the configured local agent CLI."""

from __future__ import annotations

import importlib.resources
import json
import threading
from typing import Any, Callable

from tg_collab.agent_runner import AgentError, AgentRunner, default_agent_spec
from tg_collab.config import AppPaths, get_language
from tg_collab.db import init_db
from tg_collab.services import profile_json
from tg_collab.store import (
    get_channel,
    get_posts,
    get_profile,
    get_profile_by_id,
    save_draft,
    set_status,
)


def load_prompt_template() -> str:
    return (
        importlib.resources.files("tg_collab")
        .joinpath("prompts/draft.md")
        .read_text(encoding="utf-8")
    )


def channel_context(conn, channel) -> dict[str, Any]:
    posts = get_posts(conn, channel.id, limit=5)
    return {
        "username": channel.username,
        "title": channel.title,
        "description": channel.description,
        "subscribers": channel.subscribers,
        "avg_views": channel.avg_views,
        "err": channel.err,
        "status": channel.status,
        "contact_status": channel.contact_status,
        "contact_value": channel.contact_value,
        "flags": channel.flags,
        "range": channel.meta.get("range_reason"),
        "posts": [
            {
                "message_id": post.message_id,
                "posted_at": post.posted_at,
                "views": post.views,
                "text": post.text,
            }
            for post in posts
        ],
    }


def build_prompt(conn, profile, channel, settings: dict[str, str]) -> str:
    context = {
        "language": "Russian" if get_language(settings) == "ru" else "English",
        "profile_json": json.dumps(profile_json(profile), ensure_ascii=False, indent=2),
        "channel_json": json.dumps(channel_context(conn, channel), ensure_ascii=False, indent=2),
    }
    return load_prompt_template().format(**context)


def run_draft_job(
    paths: AppPaths,
    settings: dict[str, str],
    channel_ids: list[int],
    progress: Callable[[int, str], None],
    cancel: threading.Event,
    profile_id: int | None = None,
) -> dict[str, Any]:
    """Generate one draft per channel through AgentRunner; never calls a network API."""
    runner = AgentRunner(default_agent_spec(settings))
    drafts = 0
    errors: list[dict[str, str]] = []
    conn = init_db(paths.db_path)
    try:
        profile = (
            get_profile_by_id(conn, profile_id) if profile_id is not None else get_profile(conn)
        )
        if profile is None:
            raise RuntimeError("no profile yet - run `tg-collab init @your_channel` first")
        total = max(len(channel_ids), 1)
        for index, channel_id in enumerate(channel_ids):
            if cancel.is_set():
                break
            percent = int(100 * index / total)
            channel = get_channel(conn, channel_id=channel_id, profile_id=profile_id)
            if channel is None:
                errors.append({"username": f"id:{channel_id}", "error": "channel not found"})
                progress(percent, f"id:{channel_id}: channel not found")
                continue
            progress(percent, f"@{channel.username}: asking the local agent")
            try:
                text = runner.run(build_prompt(conn, profile, channel, settings))
            except AgentError as exc:
                errors.append({"username": channel.username, "error": str(exc)})
                progress(percent, f"@{channel.username}: {exc}")
                continue
            save_draft(conn, channel.id, text)
            if channel.status in ("found", "shortlisted"):
                set_status(conn, channel.username, "drafted", profile_id=profile_id)
            drafts += 1
            progress(int(100 * (index + 1) / total), f"@{channel.username}: draft saved")

        if errors and drafts == 0 and len(channel_ids) == 1:
            raise AgentError(errors[0]["error"])
        return {"drafts": drafts, "errors": errors}
    finally:
        conn.close()
