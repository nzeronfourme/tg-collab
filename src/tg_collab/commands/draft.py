"""``tg-collab draft`` - save a personal intro for one channel."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from tg_collab.commands.common import open_db
from tg_collab.errors import TgCollabError
from tg_collab.models import STATUSES
from tg_collab.output import emit_json
from tg_collab.store import get_channel, save_draft, set_status


def register(app: typer.Typer) -> None:
    @app.command("draft")
    def draft_command(
        ctx: typer.Context,
        channel: str = typer.Argument(..., help="Channel username, e.g. @alpha."),
        text: str | None = typer.Argument(None, help="Draft text; omit when using --file."),
        file: Path | None = typer.Option(None, "--file", help="Read the draft from a UTF-8 file."),
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    ) -> None:
        """Save a draft; only found/shortlisted channels move to drafted."""
        if file is not None:
            if not file.is_file():
                raise TgCollabError(f"file not found: {file}")
            text = file.read_text(encoding="utf-8")
        if text is None or not text.strip():
            raise TgCollabError("provide the draft text or --file")

        conn = open_db(ctx)
        try:
            stored = get_channel(conn, username=channel)
            if stored is None:
                raise TgCollabError(f"channel not found: @{channel.lstrip('@')}")
            draft = save_draft(conn, stored.id, text)
            new_status = stored.status
            if stored.status in ("found", "shortlisted"):
                new_status = set_status(conn, stored.username, "drafted").status
            assert new_status in STATUSES
        finally:
            conn.close()

        payload = {
            "username": stored.username,
            "draft_id": draft.id,
            "status": new_status,
            "text": draft.text,
        }
        if json_output:
            emit_json(payload)
        else:
            Console().print(Panel(draft.text, title=f"@{stored.username} -> {new_status}", expand=False))
