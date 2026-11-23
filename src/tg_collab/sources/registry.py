"""Build enabled providers from settings; optional sources fail softly."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    module: str
    factory: str


#: Optional discovery sources. Telegram Public (the mention graph) is the base
#: source and is not a toggleable provider; these add candidates on top of it.
DISCOVERY_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("web_search", "tg_collab.sources.web_search", "build_provider"),
    ProviderSpec("tgstat", "tg_collab.sources.tgstat", "build_discovery_provider"),
    ProviderSpec("agent_search", "tg_collab.sources.agent_search", "build_provider"),
    ProviderSpec("search_hn", "tg_collab.sources.hn", "build_provider"),
)

ENRICHMENT_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("tgstat", "tg_collab.sources.tgstat", "build_enrichment_provider"),
)


def _instantiate(
    spec: ProviderSpec,
    settings: dict[str, str],
    http: Any,
    warnings: list[str],
) -> Any | None:
    try:
        module = importlib.import_module(spec.module)
    except ModuleNotFoundError as exc:
        if exc.name == spec.module:
            return None  # this optional source is not part of the installed build yet
        warnings.append(f"{spec.name} unavailable: missing {exc.name}")
        return None
    except Exception as exc:  # noqa: BLE001 - optional imports must never crash a run
        warnings.append(f"{spec.name} unavailable: {type(exc).__name__}: {exc}")
        return None
    factory = getattr(module, spec.factory, None)
    if not callable(factory):
        warnings.append(f"{spec.name} unavailable: {spec.factory} is missing")
        return None
    try:
        return factory(settings, http)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{spec.name} unavailable: {type(exc).__name__}: {exc}")
        return None


def _build(
    specs: tuple[ProviderSpec, ...],
    settings: dict[str, str],
    http: Any,
    warnings: list[str],
) -> list[Any]:
    built: list[Any] = []
    for spec in specs:
        provider = _instantiate(spec, settings, http, warnings)
        if provider is None:
            continue
        try:
            if not provider.enabled(settings):
                continue
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{spec.name} disabled: {type(exc).__name__}: {exc}")
            continue
        built.append(provider)
    return built


def _append_plugins(
    providers: list[Any],
    settings: dict[str, str],
    http: Any,
    warnings: list[str],
) -> None:
    try:
        from tg_collab.sources.plugins import load_plugins
    except ModuleNotFoundError as exc:
        if (exc.name or "").startswith("tg_collab.sources.plugins"):
            return
        warnings.append(f"plugins unavailable: missing {exc.name}")
        return
    try:
        providers.extend(load_plugins(settings, http))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"plugins unavailable: {type(exc).__name__}: {exc}")


def build_discovery_providers(
    settings: dict[str, str],
    http: Any = None,
    warnings: list[str] | None = None,
    specs: tuple[ProviderSpec, ...] | None = None,
) -> list[Any]:
    warn = warnings if warnings is not None else []
    providers = _build(specs if specs is not None else DISCOVERY_SPECS, settings, http, warn)
    _append_plugins(providers, settings, http, warn)
    return providers


def build_enrichment_providers(
    settings: dict[str, str],
    http: Any = None,
    warnings: list[str] | None = None,
    specs: tuple[ProviderSpec, ...] | None = None,
) -> list[Any]:
    warn = warnings if warnings is not None else []
    return _build(specs if specs is not None else ENRICHMENT_SPECS, settings, http, warn)
