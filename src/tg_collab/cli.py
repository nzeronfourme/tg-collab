"""Command-line entry point, command registry, and the exit-code policy."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any

import typer

from tg_collab import __version__
from tg_collab.commands.common import guard
from tg_collab.config import resolve_paths

COMMAND_MODULES = (
    "init",
    "find",
    "list",
    "explain",
    "niche",
    "profile",
    "status",
    "draft",
    "export",
    "ui",
    "agent_prompt",
    "import_cmd",
)
HELP = "Find peer Telegram channels and run a local outreach CRM."


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


class TgCollabTyper(typer.Typer):
    """Typer app whose commands share the error/exit-code policy."""

    def command(self, *args: Any, **kwargs: Any) -> Any:
        if args and callable(args[0]) and not isinstance(args[0], str):
            return super().command(guard(args[0]), **kwargs)
        decorator = super().command(*args, **kwargs)

        def wrap(fn: Callable[..., Any]) -> Any:
            return decorator(guard(fn))

        return wrap


def _install_main(app: TgCollabTyper) -> None:
    @app.callback()
    def main(
        ctx: typer.Context,
        version: bool = typer.Option(
            False,
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
        data_dir: str | None = typer.Option(
            None,
            "--data-dir",
            help="Data directory (default: ./.tg-collab or TGCOLLAB_DATA_DIR).",
        ),
    ) -> None:
        """tg-collab - white-hat Telegram channel outreach, locally."""
        ctx.obj = resolve_paths(data_dir)


def register_module(app: typer.Typer, module: Any) -> bool:
    """Register one command module that exposes ``register(app)``."""
    register = getattr(module, "register", None)
    if not callable(register):
        return False
    register(app)
    return True


def register_commands(app: typer.Typer, names: Iterable[str] = COMMAND_MODULES) -> list[str]:
    """Import and register every command module that exists yet."""
    registered: list[str] = []
    for name in names:
        try:
            module = importlib.import_module(f"tg_collab.commands.{name}")
        except ModuleNotFoundError as exc:
            if (exc.name or "").startswith("tg_collab.commands"):
                continue
            raise
        if register_module(app, module):
            registered.append(name)
    return registered


def build_app(module_names: Iterable[str] = ()) -> TgCollabTyper:
    app = TgCollabTyper(no_args_is_help=True, add_completion=False, help=HELP)
    _install_main(app)
    register_commands(app, module_names)
    return app


app = build_app(COMMAND_MODULES)


if __name__ == "__main__":  # pragma: no cover
    app()
