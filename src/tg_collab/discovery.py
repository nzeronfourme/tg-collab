"""Breadth-first discovery over the public mention graph, with a refresh cache."""

from __future__ import annotations

import re
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from tg_collab.config import get_bool, get_int, get_setting
from tg_collab.models import Candidate
from tg_collab.similarity import build_niche_profile, rescore_channel
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.tg_public import extract_page_edges, resolve_username
from tg_collab.store import backfill_channel_languages, get_channel

SOURCE_KEYS = (
    "tg_graph",
    "tg_seed",
    "plugin_github",
    "agent_search",
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
    "tgstat",
)

#: Every source key ends a run with exactly one of these statuses.
SOURCE_STATUSES = (
    "ok",
    "empty",
    "disabled",
    "no_key",
    "captcha",
    "quota",
    "blocked",
    "error",
)
#: A run is not a total failure when a source answered but found nothing.
SOURCES_OK = frozenset({"ok", "empty"})
#: One short English remedy per status, so JSON consumers get the same advice
#: the Web UI shows in its "What to do" column.
REMEDY_BY_STATUS = {
    "ok": "",
    "empty": "Broaden the niche terms or add seed channels.",
    "disabled": "Enable or configure this source in Settings.",
    "no_key": "Set the credential for this source in Settings.",
    "captcha": "Try a different network or search engine; the CAPTCHA is never bypassed.",
    "quota": "Wait for the quota to reset or switch to another source.",
    "blocked": "Open the endpoint in a browser or change the network/proxy.",
    "error": "Check the run log and retry.",
}


def provider_source_keys(provider: Any) -> list[str]:
    """Source keys a provider owns, so a failure can be attributed to them."""
    declared = getattr(provider, "source_keys", None)
    if declared:
        return [str(key) for key in declared if str(key) in SOURCE_KEYS]
    name = str(getattr(provider, "name", "") or "")
    if name in SOURCE_KEYS:
        return [name]
    if name == "web_search":
        engines = [
            str(getattr(engine, "name", "") or "") for engine in getattr(provider, "engines", ())
        ]
        engines += list((getattr(provider, "statuses", None) or {}).keys())
        return list(
            dict.fromkeys(
                f"search_{engine}" for engine in engines if f"search_{engine}" in SOURCE_KEYS
            )
        )
    return []


def is_background_provider(provider: Any) -> bool:
    """Providers that start early, run while the graph is walked, and finish later."""
    return all(callable(getattr(provider, method, None)) for method in ("start", "finish"))


def terminate_background(provider: Any) -> None:
    """Best-effort stop of a background provider's thread or child process."""
    for method in ("cancel", "abort", "stop"):
        stop = getattr(provider, method, None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 - termination must not mask the run result
                pass
            return


@dataclass
class FindStats:
    added: int = 0
    updated: int = 0
    skipped_fresh: int = 0
    seeds: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    providers: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    sources_ok: int = 0
    source_status: dict[str, str] = field(default_factory=dict)
    source_remedy: dict[str, str] = field(default_factory=dict)


def seed_channels_for(settings: dict[str, str], profile: Any | None) -> list[str]:
    """The profile's seeds, or the legacy global setting when there is no profile.

    Migration v6 copied the legacy global value into the first profile, so a
    profile that happens to have no seeds must stay empty instead of inheriting
    another profile's global seeds.
    """
    raw = (
        str(getattr(profile, "seed_channels", "") or "")
        if profile is not None
        else get_setting(settings, "seed_channels")
    )
    return [part.strip() for part in re.split(r"[,;\n]+", raw) if part.strip()]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_fresh(checked_at: str | None, refresh_days: int) -> bool:
    parsed = _parse_dt(checked_at)
    if parsed is None:
        return False
    return (datetime.now(timezone.utc) - parsed).total_seconds() < refresh_days * 86400


class DiscoveryRun:
    """Walk the public graph from the owner's channel, enriching each new peer."""

    def __init__(
        self,
        settings: dict[str, str],
        providers: list[Any],
        public_client: Any,
        enrich: Callable[[Candidate], Any],
        store: sqlite3.Connection,
        ctx: ProviderContext | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.public_client = public_client
        self.enrich = enrich
        self.store = store
        self.ctx = ctx

    def run(
        self,
        mine_username: str,
        progress: Callable[[int, int, str], None] | None = None,
        cancel: Any = None,
    ) -> FindStats:
        backfill_channel_languages(self.store)
        stats = FindStats(providers={key: 0 for key in SOURCE_KEYS})
        statuses: dict[str, str] = {}
        assigned: set[str] = set()

        def mark(key: str, status: str) -> None:
            if key in SOURCE_KEYS and status in SOURCE_STATUSES:
                statuses[key] = status
                assigned.add(key)

        mine = resolve_username(mine_username)
        max_depth = get_int(self.settings, "find_max_depth")
        if max_depth == 0:
            max_depth = 1  # "seeds only": direct neighbours of the owner's channel
        max_channels = get_int(self.settings, "find_max_channels")
        budget = get_int(self.settings, "find_max_candidates")
        refresh_days = get_int(self.settings, "refresh_days")
        refresh_days = 7 if refresh_days is None else refresh_days

        queue: deque[tuple[Candidate, int]] = deque()
        seen: set[str] = {mine.lower()}
        buckets: dict[str, list[Candidate]] = {}
        bucket_seen: dict[str, set[str]] = {}

        def collect(candidate: Candidate) -> None:
            key = (candidate.username or "").lower()
            if not key or key in seen or key in bucket_seen.setdefault(candidate.source, set()):
                return
            bucket_seen[candidate.source].add(key)
            buckets.setdefault(candidate.source, []).append(candidate)

        def enqueue(candidate: Candidate, depth: int) -> bool:
            key = (candidate.username or "").lower()
            if not key or key in seen:
                return False
            if max_depth is not None and depth > max_depth:
                return False
            seen.add(key)
            queue.append((candidate, depth))
            return True

        backgrounds = [provider for provider in self.providers if is_background_provider(provider)]
        normal = [
            provider for provider in self.providers if not is_background_provider(provider)
        ]
        cancelled = cancel is not None and cancel.is_set()
        running: list[Any] = []
        for provider in backgrounds:
            name = getattr(provider, "name", type(provider).__name__)
            if cancelled:
                stats.warnings.append(f"cancelled before {name} started")
                for key in provider_source_keys(provider):
                    mark(key, "error")
                continue
            try:
                provider.start(self.ctx)
            except Exception as exc:  # noqa: BLE001 - one source must not abort the run
                stats.errors.append({"username": f"provider:{name}", "error": str(exc)})
                for key in provider_source_keys(provider):
                    mark(key, "error")
                terminate_background(provider)
                continue
            running.append(provider)

        def absorb(provider: Any, candidates: list[Candidate]) -> None:
            if self.ctx is not None:
                for key, status in (getattr(provider, "statuses", None) or {}).items():
                    mark(key if key in SOURCE_KEYS else f"search_{key}", status)
                for key, status in self.ctx.statuses.items():
                    mark(key, status)
            keys = provider_source_keys(provider)
            counts: dict[str, int] = {}
            for candidate in candidates:
                counts[candidate.source] = counts.get(candidate.source, 0) + 1
                collect(candidate)
            for key in keys:
                if key not in assigned:
                    mark(key, "ok" if counts.get(key) else "empty")
            for source, count in counts.items():
                if count and source in SOURCE_KEYS and source not in assigned:
                    mark(source, "ok")

        if get_bool(self.settings, "sources_tg_public"):
            try:
                own_page = self.public_client.fetch_page(mine)
            except Exception as exc:  # noqa: BLE001 - the graph is one source among many
                stats.errors.append({"username": "provider:tg_public", "error": str(exc)})
                mark("tg_graph", "error")
            else:
                edges = sorted(extract_page_edges(own_page))
                for edge in edges:
                    collect(Candidate(username=edge, source="tg_graph"))
                mark("tg_graph", "ok" if edges else "empty")

        seed_configured = False
        profile = self.ctx.profile if self.ctx is not None else None
        for raw_seed in seed_channels_for(self.settings, profile):
            seed = raw_seed.strip()
            if not seed:
                continue
            seed_configured = True
            try:
                seed_name = resolve_username(seed)
            except ValueError:
                stats.warnings.append(f"ignored seed channel: {seed}")
                continue
            collect(Candidate(username=seed_name, source="tg_seed"))
        if seed_configured:
            mark("tg_seed", "ok" if bucket_seen.get("tg_seed") else "empty")

        for provider in normal:
            name = getattr(provider, "name", type(provider).__name__)
            try:
                candidates = (
                    provider.discover(self.ctx)
                    if self.ctx is not None
                    else provider.discover(None)
                )
            except Exception as exc:  # noqa: BLE001 - one source must not abort the run
                stats.errors.append({"username": f"provider:{name}", "error": str(exc)})
                for key in provider_source_keys(provider):
                    mark(key, "error")
                continue
            absorb(provider, candidates)

        cancelled = cancelled or (cancel is not None and cancel.is_set())
        for provider in running:
            name = getattr(provider, "name", type(provider).__name__)
            keys = provider_source_keys(provider)
            if cancelled:
                terminate_background(provider)
                stats.warnings.append(f"cancelled before {name} finished")
                for key in keys:
                    mark(key, "error")
                continue
            try:
                candidates = provider.finish(self.ctx)
            except Exception as exc:  # noqa: BLE001 - a late source failure is not fatal
                stats.errors.append({"username": f"provider:{name}", "error": str(exc)})
                terminate_background(provider)
                stats.warnings.append(f"{name}: background provider did not finish: {exc}")
                for key in keys:
                    mark(key, "error")
                continue
            absorb(provider, candidates)

        initial = 0
        iterators = [iter(items) for items in buckets.values()]
        while iterators:
            for iterator in list(iterators):
                try:
                    candidate = next(iterator)
                except StopIteration:
                    iterators.remove(iterator)
                    continue
                if budget is not None and initial >= budget:
                    stats.warnings.append(
                        f"stopped at find_max_candidates={budget}; more candidates remain"
                    )
                    iterators.clear()
                    break
                if not enqueue(candidate, 1):
                    continue
                initial += 1
                if candidate.source == "tg_seed":
                    stats.seeds += 1
                stats.providers[candidate.source] = stats.providers.get(candidate.source, 0) + 1

        processed = 0
        niche = None
        while queue:
            if cancel is not None and cancel.is_set():
                stats.warnings.append("cancelled before the next channel")
                break
            if max_channels is not None and processed >= max_channels:
                stats.warnings.append(
                    f"stopped at find_max_channels={max_channels}; more channels remain queued"
                )
                break
            candidate, depth = queue.popleft()
            username = candidate.username

            existing = get_channel(
                self.store,
                username=username,
                profile_id=self.ctx.profile_id if self.ctx is not None else None,
            )
            if existing is not None and _is_fresh(existing.checked_at, refresh_days):
                stats.skipped_fresh += 1
                if self.ctx is not None and self.ctx.profile is not None:
                    if niche is None:
                        niche = build_niche_profile(self.ctx.profile)
                    rescore_channel(
                        self.store,
                        self.ctx.profile,
                        self.settings,
                        username,
                        niche=niche,
                        profile_id=self.ctx.profile_id,
                    )
                for edge in existing.meta.get("edges") or []:
                    if enqueue(Candidate(username=str(edge), source="tg_graph"), depth + 1):
                        stats.providers["tg_graph"] += 1
                continue

            if progress is not None:
                progress(processed, processed + len(queue) + 1, f"@{username}")
            try:
                channel = self.enrich(candidate)
            except Exception as exc:  # noqa: BLE001 - a single failure is reported, not fatal
                stats.errors.append({"username": username, "error": str(exc)})
                continue
            if channel is None:
                stats.errors.append({"username": username, "error": "enrichment returned nothing"})
                continue
            processed += 1
            if existing is None:
                stats.added += 1
            else:
                stats.updated += 1
            for edge in channel.meta.get("edges") or []:
                if enqueue(Candidate(username=str(edge), source="tg_graph"), depth + 1):
                    stats.providers["tg_graph"] += 1

        if self.ctx is not None:
            stats.warnings.extend(self.ctx.warnings)
        stats.source_status = {
            key: statuses.get(key, "disabled") for key in SOURCE_KEYS
        }
        stats.source_remedy = {
            key: REMEDY_BY_STATUS[status] for key, status in stats.source_status.items()
        }
        stats.sources_ok = sum(
            1 for status in stats.source_status.values() if status in SOURCES_OK
        )
        return stats
