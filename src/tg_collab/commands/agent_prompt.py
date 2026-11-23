"""``tg-collab agent-prompt`` - hand a research task to the user's local AI agent."""

from __future__ import annotations

import importlib.resources
import json
import re
from collections.abc import Sequence
from string import Template
from typing import Final

import typer

from tg_collab.commands.common import open_db_with_settings
from tg_collab.config import get_language, get_setting
from tg_collab.errors import TgCollabError
from tg_collab.output import emit_json
from tg_collab.reports import target_range
from tg_collab.services import profile_json, range_json
from tg_collab.similarity import niche_tags, profile_posts
from tg_collab.store import get_profile

SCHEMA = [{"username": "channel_username", "url": "https://t.me/channel_username", "why": "one sentence"}]

#: The research template ships 8 sampled posts and caps each at 400 characters.
SAMPLE_LIMIT = 8
#: The compact automatic-search brief keeps only the four strongest samples.
SEARCH_SAMPLE_LIMIT = 4
SAMPLE_MAX_CHARS = 400
SUPPORTED_LANGUAGES = ("en", "ru")
LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}
#: A post must never be able to close or reopen the untrusted-data block.
_SAMPLE_DELIMITER_RE: Final[re.Pattern[str]] = re.compile(r"(samples)-(begin|end)", re.IGNORECASE)


def load_research_template(language: str) -> str:
    """Read the packaged research brief for the profile language."""
    chosen = language if language in SUPPORTED_LANGUAGES else "en"
    return (
        importlib.resources.files("tg_collab")
        .joinpath(f"prompts/research.{chosen}.md")
        .read_text(encoding="utf-8")
    )


def load_search_template(language: str) -> str:
    """Read the packaged compact search brief for the profile language."""
    chosen = language if language in SUPPORTED_LANGUAGES else "en"
    return (
        importlib.resources.files("tg_collab")
        .joinpath(f"prompts/search.{chosen}.md")
        .read_text(encoding="utf-8")
    )


def post_samples(
    profile,
    limit: int = SAMPLE_LIMIT,
    max_chars: int = SAMPLE_MAX_CHARS,
) -> list[str]:
    """Recent post texts as quoted samples, capped like the brief promises."""
    samples: list[str] = []
    for item in profile_posts(profile):
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        text = _SAMPLE_DELIMITER_RE.sub(r"\1_\2", text)
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        samples.append(text)
        if len(samples) >= limit:
            break
    return samples


def _samples_block(samples: Sequence[str], language: str) -> str:
    if not samples:
        return "(no posts available)" if language == "en" else "(постов нет)"
    return "\n".join(
        f"{index}. <<<{text}>>>" for index, text in enumerate(samples, start=1)
    )


def _size_range(profile, target, language: str) -> str:
    basis = profile.subscribers or 0
    if language == "ru":
        line = f"Целевой размер: {target.low}-{target.high} подписчиков"
        if basis:
            return f"{line} ({target.min_mult:g}x-{target.max_mult:g}x от {basis})."
        return line + "."
    line = f"Target size: {target.low}-{target.high} subscribers"
    if basis:
        return f"{line} ({target.min_mult:g}x-{target.max_mult:g}x of {basis})."
    return line + "."


def build_task(
    profile,
    target,
    terms: list[str],
    language: str,
    *,
    hashtags: Sequence[str] = (),
    domains: Sequence[str] = (),
    region: str = "",
) -> str:
    """Render the packaged research brief for the owner's local agent."""
    chosen = language if language in SUPPORTED_LANGUAGES else "en"
    samples = post_samples(profile)
    values = {
        "USERNAME": profile.username,
        "PROFILE_JSON": json.dumps(profile_json(profile), ensure_ascii=False, indent=2),
        "POST_SAMPLES": _samples_block(samples, chosen),
        "TERMS": ", ".join(terms) or "-",
        "HASHTAGS": ", ".join(f"#{tag}" for tag in hashtags) or "-",
        "DOMAINS": ", ".join(domains) or "-",
        "LANGUAGE": LANGUAGE_NAMES[chosen],
        "REGION": region or "-",
        "SIZE_RANGE": _size_range(profile, target, chosen),
        "SCHEMA": json.dumps(SCHEMA, ensure_ascii=False),
    }
    return Template(load_research_template(chosen)).substitute(values)


def build_search_task(
    profile,
    target,
    terms: list[str],
    language: str,
    *,
    hashtags: Sequence[str] = (),
    domains: Sequence[str] = (),
    region: str = "",
) -> str:
    """Render the compact, bounded brief the automatic agent search runs."""
    chosen = language if language in SUPPORTED_LANGUAGES else "en"
    samples = post_samples(profile, limit=SEARCH_SAMPLE_LIMIT)
    values = {
        "USERNAME": profile.username,
        "PROFILE_JSON": json.dumps(profile_json(profile), ensure_ascii=False, indent=2),
        "POST_SAMPLES": _samples_block(samples, chosen),
        "TERMS": ", ".join(terms) or "-",
        "HASHTAGS": ", ".join(f"#{tag}" for tag in hashtags) or "-",
        "DOMAINS": ", ".join(domains) or "-",
        "LANGUAGE": LANGUAGE_NAMES[chosen],
        "REGION": region or "-",
        "SIZE_RANGE": _size_range(profile, target, chosen),
    }
    return Template(load_search_template(chosen)).substitute(values)


def register(app: typer.Typer) -> None:
    @app.command("agent-prompt")
    def agent_prompt_command(
        ctx: typer.Context,
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    ) -> None:
        """Print a research task for the local AI agent."""
        conn, settings = open_db_with_settings(ctx)
        try:
            profile = get_profile(conn)
            if profile is None:
                raise TgCollabError("no profile yet - run `tg-collab init @your_channel` first")
            target = target_range(profile, settings)
            assert target is not None
            language = get_language(settings)
            terms, tags, domains = niche_tags(profile)
            prompt = build_task(
                profile,
                target,
                terms,
                language,
                hashtags=tags,
                domains=domains,
                region=get_setting(settings, "search_region"),
            )
            payload = {
                "prompt": prompt,
                "profile": profile_json(profile),
                "range": range_json(target),
                "niche_terms": terms,
                "hashtags": tags,
                "link_domains": domains,
                "schema": SCHEMA,
                "language": language,
                "template": f"research.{language}",
                "posts_sample": post_samples(profile),
            }
        finally:
            conn.close()

        if json_output:
            emit_json(payload)
        else:
            typer.echo(prompt)
