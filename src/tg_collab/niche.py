"""The agent-defined niche profile: prompt, strict validation, storage, and generation.

The stored profile lives in ``profile.niche_json``. A stored and valid niche
takes precedence over the deterministic extractor in ``similarity``; an empty
or invalid value falls back to those deterministic tags without overwriting the
stored text.

This module stays free of top-level imports from ``similarity`` and ``services``
so that ``similarity`` can import it for the effective tags.
"""

from __future__ import annotations

import importlib.resources
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from functools import lru_cache
from string import Template
from typing import Any, Callable

from tg_collab.agent_runner import AgentError, AgentRunner, default_agent_spec
from tg_collab.config import get_bool, get_language, proxy_env
from tg_collab.models import Profile, utcnow
from tg_collab.store import get_profile, get_profile_by_id, set_profile_niche

TERM_LIMIT = 12
HASHTAG_LIMIT = 5
DOMAIN_LIMIT = 5
STOPWORD_LIMIT = 5
#: The prompt asks for 8-12 terms; the validator accepts a smaller honest list
#: (the expected quality bar for the owner's channel has seven).
AGENT_MIN_TERMS = 3
SUMMARY_LIMIT = 300
SAMPLE_LIMIT = 12
SAMPLE_MAX_CHARS = 320
TERM_MAX_CHARS = 40
TERM_MAX_WORDS = 4
TOKEN_MAX_CHARS = 30
SUPPORTED_LANGUAGES = ("en", "ru")
LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}
NICHE_SOURCES = ("agent", "manual")

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*|\s*```$")
_TERM_REJECT_RE = re.compile(r"://|t\.me|@|#", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")
_HASHTAG_RE = re.compile(r"[^A-Za-zА-Яа-яЁё0-9_]+")


@dataclass(frozen=True)
class Niche:
    """A validated niche profile, as stored in ``profile.niche_json``."""

    terms: tuple[str, ...]
    hashtags: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    stopwords: tuple[str, ...] = ()
    summary: str = ""
    source: str = "agent"
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "generated_at": self.generated_at,
            "terms": list(self.terms),
            "hashtags": list(self.hashtags),
            "domains": list(self.domains),
            "stopwords": list(self.stopwords),
            "summary": self.summary,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _string_list(value: Any, limit: int) -> list[str] | None:
    """A list of trimmed, non-empty, deduplicated strings, or ``None`` for a bad shape."""
    if not isinstance(value, list):
        return None
    items: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            return None
        text = " ".join(raw.split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _clean_term(raw: str) -> str | None:
    term = " ".join(raw.split()).lower()
    if not term or _TERM_REJECT_RE.search(term):
        return None
    return term


def _clean_hashtag(raw: str) -> str | None:
    tag = _HASHTAG_RE.sub("", raw.lstrip("#")).lower()
    return tag or None


def _term_is_sane(term: str) -> bool:
    if len(term) > TERM_MAX_CHARS or len(term.split()) > TERM_MAX_WORDS:
        return False
    if " " not in term:
        return len(term) <= TOKEN_MAX_CHARS and term.count("_") <= 1
    return True


def _clean_domain(raw: str) -> str | None:
    domain = raw.strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain if _DOMAIN_RE.match(domain) else None


def validate_niche(
    payload: Any,
    *,
    min_terms: int = 1,
    default_source: str = "agent",
    generated_at: str | None = None,
) -> Niche | None:
    """Validate an agent or manual payload; ``None`` means it is not usable.

    Optional sections must still have the right shape when present, so a partial
    answer is rejected instead of being half-applied.
    """
    if not isinstance(payload, dict):
        return None
    supplied_terms = payload.get("terms")
    if isinstance(supplied_terms, list) and sum(
        1 for item in supplied_terms if isinstance(item, str) and item.strip()
    ) > TERM_LIMIT:
        return None
    raw_terms = _string_list(supplied_terms, TERM_LIMIT)
    if raw_terms is None:
        return None
    terms: list[str] = []
    for raw in raw_terms:
        term = _clean_term(raw)
        if term is None:
            return None
        if not _term_is_sane(term):
            continue
        if term not in terms:
            terms.append(term)
    if not (min_terms <= len(terms) <= TERM_LIMIT):
        return None

    hashtags: list[str] = []
    if "hashtags" in payload:
        raw_tags = _string_list(payload.get("hashtags"), HASHTAG_LIMIT)
        if raw_tags is None:
            return None
        for raw in raw_tags:
            for piece in raw.split("#"):
                tag = _clean_hashtag(piece)
                if tag is not None and tag not in hashtags:
                    hashtags.append(tag)
                if len(hashtags) >= HASHTAG_LIMIT:
                    break
            if len(hashtags) >= HASHTAG_LIMIT:
                break

    domains: list[str] = []
    if "domains" in payload:
        raw_domains = _string_list(payload.get("domains"), DOMAIN_LIMIT)
        if raw_domains is None:
            return None
        for raw in raw_domains:
            domain = _clean_domain(raw)
            if domain is not None and domain not in domains:
                domains.append(domain)

    stopwords: list[str] = []
    if "stopwords" in payload:
        raw_stopwords = _string_list(payload.get("stopwords"), STOPWORD_LIMIT)
        if raw_stopwords is None:
            return None
        stopwords = [word.lower() for word in raw_stopwords]

    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        return None
    summary = " ".join(summary.split())[:SUMMARY_LIMIT]

    source = payload.get("source")
    if source not in NICHE_SOURCES:
        source = default_source
    stored_at = payload.get("generated_at")
    if not isinstance(stored_at, str):
        stored_at = generated_at or ""

    return Niche(
        terms=tuple(terms),
        hashtags=tuple(hashtags),
        domains=tuple(domains),
        stopwords=tuple(stopwords),
        summary=summary,
        source=source,
        generated_at=stored_at,
    )


def parse_niche(raw: str | None, *, min_terms: int = 1) -> Niche | None:
    """Parse one JSON document (optionally fenced) and validate its shape strictly."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return validate_niche(payload, min_terms=min_terms)


def load_niche(profile: Profile | None) -> Niche | None:
    """The stored niche when it is valid; ``None`` for empty or broken text."""
    if profile is None:
        return None
    return parse_niche(profile.niche_json)


def save_niche(conn: sqlite3.Connection, niche: Niche, profile_id: int | None = None) -> None:
    set_profile_niche(conn, niche.to_json(), profile_id=profile_id)


def clear_niche(conn: sqlite3.Connection, profile_id: int | None = None) -> None:
    set_profile_niche(conn, "", profile_id=profile_id)


def make_manual_niche(
    terms: list[str], previous: Niche | None = None, *, generated_at: str | None = None
) -> Niche | None:
    """Build a manual niche, keeping the previous optional sections when present."""
    payload: dict[str, Any] = {"terms": terms, "source": "manual"}
    if previous is not None:
        payload.update(
            {
                "hashtags": list(previous.hashtags),
                "domains": list(previous.domains),
                "stopwords": list(previous.stopwords),
                "summary": previous.summary,
            }
        )
    return validate_niche(
        payload,
        min_terms=1,
        default_source="manual",
        generated_at=generated_at or utcnow(),
    )


@lru_cache(maxsize=2)
def load_niche_template(language: str) -> str:
    """Read the packaged niche brief for the requested language."""
    chosen = language if language in SUPPORTED_LANGUAGES else "en"
    return (
        importlib.resources.files("tg_collab")
        .joinpath(f"prompts/niche.{chosen}.md")
        .read_text(encoding="utf-8")
    )


def _profile_payload(profile: Profile) -> dict[str, Any]:
    from tg_collab.services import profile_json

    return profile_json(profile)


def _samples_block(samples: list[str], language: str) -> str:
    if not samples:
        return "(no posts available)" if language == "en" else "(постов нет)"
    return "\n".join(f"{index}. <<<{text}>>>" for index, text in enumerate(samples, start=1))


def _pinned_block(profile: Profile, language: str) -> str:
    pinned = (profile.pinned_text or "").strip()
    if pinned:
        return f"<<<{pinned}>>>"
    return "(no pinned post)" if language == "en" else "(закреплённого поста нет)"


def build_niche_prompt(profile: Profile, language: str) -> str:
    """Render the packaged niche brief for the owner's local agent."""
    from tg_collab.commands.agent_prompt import post_samples

    chosen = language if language in SUPPORTED_LANGUAGES else "en"
    samples = post_samples(profile, limit=SAMPLE_LIMIT, max_chars=SAMPLE_MAX_CHARS)
    values = {
        "USERNAME": profile.username,
        "PROFILE_JSON": json.dumps(_profile_payload(profile), ensure_ascii=False, indent=2),
        "PINNED_POST": _pinned_block(profile, chosen),
        "POST_SAMPLES": _samples_block(samples, chosen),
        "LANGUAGE": LANGUAGE_NAMES[chosen],
    }
    return Template(load_niche_template(chosen)).substitute(values)


def generate_niche(
    settings: dict[str, str],
    profile: Profile,
    cancel: Any = None,
    runner_factory: Callable[[Any], Any] | None = None,
) -> tuple[Niche | None, str | None]:
    """Ask the configured agent for a niche profile; never raises ``AgentError``.

    The agent command and timeout are the existing ``agent_command`` /
    ``agent_timeout_sec`` pair, the same ones the draft and search paths use.
    Returns the niche and ``None``, or ``None`` and a warning that names why.
    """
    prompt = build_niche_prompt(profile, get_language(settings))
    try:
        spec = default_agent_spec(settings)
        runner = (runner_factory or AgentRunner)(spec)
        raw = runner.run(prompt, cancel=cancel, env=proxy_env(settings))
    except AgentError as exc:
        return None, f"niche: the agent did not answer ({exc}); the deterministic terms stay in use"
    niche = parse_niche(raw, min_terms=AGENT_MIN_TERMS)
    if niche is None:
        return None, (
            "niche: the agent answer was not a valid niche profile; "
            "the deterministic terms stay in use"
        )
    if not niche.generated_at:
        niche = replace(niche, generated_at=utcnow())
    return niche, None


def ensure_niche(
    conn: sqlite3.Connection,
    settings: dict[str, str],
    profile: Profile | None,
    cancel: Any = None,
    runner_factory: Callable[[Any], Any] | None = None,
    profile_id: int | None = None,
) -> tuple[Profile | None, list[str]]:
    """Fill an empty niche from the agent once; return the refreshed profile and warnings.

    A stored value, even an invalid one, is never overwritten here: the
    deterministic extractor handles a broken value and the owner can repair it
    with ``tg-collab niche``. The automatic-agent toggle is respected so a run
    with ``sources_agent_search=0`` stays fully deterministic.
    """
    if profile is None or (profile.niche_json or "").strip():
        return profile, []
    if not get_bool(settings, "sources_agent_search"):
        return profile, []
    niche, warning = generate_niche(
        settings, profile, cancel=cancel, runner_factory=runner_factory
    )
    if niche is None:
        return profile, [warning] if warning else []
    save_niche(conn, niche, profile_id=profile_id)
    refreshed = (
        get_profile_by_id(conn, profile_id) if profile_id is not None else get_profile(conn)
    )
    return refreshed or profile, []
