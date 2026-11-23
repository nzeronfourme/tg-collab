"""Helpers shared by CLI command modules."""

from __future__ import annotations

import functools
import os
import sqlite3
from collections.abc import Callable
from typing import Any

import typer

from tg_collab.config import AppPaths, ensure_home, load_settings, resolve_paths
from tg_collab.db import init_db
from tg_collab.errors import TgCollabError
from tg_collab.output import write_stderr


def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn TgCollabError into the documented exit code and keep stdout clean."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except TgCollabError as exc:
            write_stderr(f"error: {exc.message}")
            raise SystemExit(exc.exit_code) from None
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            if os.environ.get("TGCOLLAB_DEBUG") == "1":
                raise
            write_stderr(f"error: {exc}")
            raise SystemExit(1) from None

    return wrapper


def get_paths(ctx: typer.Context) -> AppPaths:
    obj = getattr(ctx, "obj", None)
    return obj if isinstance(obj, AppPaths) else resolve_paths()


def open_db(ctx: typer.Context) -> sqlite3.Connection:
    paths = get_paths(ctx)
    ensure_home(paths)
    return init_db(paths.db_path)


def open_db_with_settings(ctx: typer.Context) -> tuple[sqlite3.Connection, dict[str, str]]:
    conn = open_db(ctx)
    return conn, load_settings(conn)
