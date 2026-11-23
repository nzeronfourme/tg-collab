"""``tg-collab status`` - change the outreach status of a channel."""

from __future__ import annotations

import typer

from tg_collab.commands.common import open_db
from tg_collab.errors import TgCollabError
from tg_collab.output import emit_json
from tg_collab.store import set_status


def register(app: typer.Typer) -> None:
    @app.command("status")
    def status_command(
        ctx: typer.Context,
        channel: str = typer.Argument(..., help="Channel username, e.g. @alpha."),
        new_status: str = typer.Argument(..., help="found|shortlisted|drafted|sent|replied|agreed|done|ignored"),
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    ) -> None:
        """Update a channel's outreach status."""
        conn = open_db(ctx)
        try:
            try:
                stored = set_status(conn, channel, new_status)
            except ValueError as exc:
                raise TgCollabError(str(exc)) from None
            except LookupError as exc:
                raise TgCollabError(str(exc)) from None
        finally:
            conn.close()

        payload = {"username": stored.username, "status": stored.status}
        if json_output:
            emit_json(payload)
        else:
            typer.echo(f"@{stored.username} -> {stored.status}")
