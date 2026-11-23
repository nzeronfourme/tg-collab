"""Built-in discovery plugins."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from tg_collab.sources.plugins.base import BaseDiscoveryPlugin
from tg_collab.sources.plugins.github import GithubPlugin

logger = logging.getLogger(__name__)

BUILTIN_PLUGINS: tuple[type[BaseDiscoveryPlugin], ...] = (GithubPlugin,)


def load_plugins(
    settings: dict[str, str],
    http: Any = None,
    plugins: Iterable[type[BaseDiscoveryPlugin]] | None = None,
) -> list[BaseDiscoveryPlugin]:
    loaded: list[BaseDiscoveryPlugin] = []
    for plugin_class in plugins if plugins is not None else BUILTIN_PLUGINS:
        try:
            plugin = plugin_class(settings, http)
            if plugin.enabled(settings):
                loaded.append(plugin)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not break the run
            logger.warning("plugin %s could not start: %s", plugin_class.__name__, exc)
    return loaded


__all__ = ["BUILTIN_PLUGINS", "BaseDiscoveryPlugin", "GithubPlugin", "load_plugins"]
