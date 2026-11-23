"""Provider protocols and the per-run context passed to every provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from tg_collab.models import Candidate, ChannelPage, Profile


@dataclass
class ProviderContext:
    settings: dict[str, str]
    http: Any
    profile: Profile | None = None
    store: Any | None = None
    warnings: list[str] = field(default_factory=list)
    enrichers: list[Any] | None = None
    statuses: dict[str, str] = field(default_factory=dict)
    profile_id: int | None = None
    #: The run's cancel event, so a long background source can stop promptly.
    cancel: Any = None


class DiscoveryProvider(Protocol):
    name: str

    def enabled(self, settings: dict[str, str]) -> bool: ...

    def discover(self, ctx: ProviderContext) -> list[Candidate]: ...


class EnrichmentProvider(Protocol):
    name: str

    def enabled(self, settings: dict[str, str]) -> bool: ...

    def enrich(self, username: str) -> ChannelPage | None: ...


class SearchEngine(Protocol):
    name: str
    #: Cache-row identity; defaults to ``name`` for engines that do not override it.
    cache_key: str
    #: Query budget per run; 0 means every query from the profile.
    max_queries: int

    def search(self, query: str, limit: int) -> list[str]: ...
