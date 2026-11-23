"""``tg-collab ui`` - start the local server in the background and open the browser."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx
import typer

from tg_collab.commands.common import get_paths, open_db_with_settings
from tg_collab.config import AppPaths, get_int
from tg_collab.errors import TgCollabError

POLL_TIMEOUT = 15.0
POLL_INTERVAL = 0.2
PROBE_TIMEOUT = 0.5

sleep = time.sleep
clock = time.monotonic
open_browser = webbrowser.open


def probe(url: str) -> bool:
    try:
        response = httpx.get(f"{url}/api/health", timeout=PROBE_TIMEOUT)
        return response.status_code == 200 and response.json().get("status") == "ok"
    except Exception:  # noqa: BLE001 - a missing server is the normal path
        return False


def spawn_command(paths: AppPaths, port: int) -> tuple[list[str], dict[str, str], Path]:
    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "tg_collab.web.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    env = os.environ.copy()
    env["TGCOLLAB_DATA_DIR"] = str(paths.home)
    return argv, env, paths.home / "server.log"


def spawn(paths: AppPaths, port: int) -> subprocess.Popen:
    argv, env, log_path = spawn_command(paths, port)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"cwd": str(Path.cwd()), "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with log_path.open("ab") as log:
        return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, **kwargs)


def wait_for_health(url: str, timeout: float | None = None) -> bool:
    limit = POLL_TIMEOUT if timeout is None else timeout
    deadline = clock() + limit
    while clock() < deadline:
        if probe(url):
            return True
        sleep(POLL_INTERVAL)
    return False


def log_tail(log_path: Path, limit: int = 2000) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def register(app: typer.Typer) -> None:
    @app.command("ui")
    def ui_command(
        ctx: typer.Context,
        port: int | None = typer.Option(None, "--port", help="Override the configured UI port."),
        no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
    ) -> None:
        """Start the local web UI in the background and open it."""
        paths = get_paths(ctx)
        conn, settings = open_db_with_settings(ctx)
        conn.close()
        chosen = port if port is not None else (get_int(settings, "ui_port") or 8080)
        url = f"http://127.0.0.1:{chosen}"
        log_path = paths.home / "server.log"

        if probe(url):
            if not no_browser:
                open_browser(url)
            typer.echo(url)
            return

        try:
            spawn(paths, chosen)
        except OSError as exc:
            raise TgCollabError(
                f"could not start the server: {exc}; see {log_path}"
            ) from None

        if wait_for_health(url):
            if not no_browser:
                open_browser(url)
            typer.echo(url)
            return

        tail = log_tail(log_path)
        raise TgCollabError(f"server did not start in time; see {log_path}\n{tail}")
