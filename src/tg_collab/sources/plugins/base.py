"""Discovery plugin contract: one class per external source."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from tg_collab.models import Candidate
from tg_collab.sources.base import ProviderContext


class BaseDiscoveryPlugin(ABC):
    """Implement ``enabled`` and ``discover``; the registry does the rest."""

    name: str = "plugin"

    def __init__(self, settings: dict[str, str], http: Any = None) -> None:
        self.settings = settings
        self.http = http
        self.warnings: list[str] = []

    @abstractmethod
    def enabled(self, settings: dict[str, str]) -> bool: ...

    @abstractmethod
    def discover(self, ctx: ProviderContext) -> list[Candidate]: ...
