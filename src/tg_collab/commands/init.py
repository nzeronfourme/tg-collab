"""``tg-collab init`` - profile the owner's channel and derive the target range."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tg_collab.commands.common import open_db_with_settings
from tg_collab.output import emit_json, human_float, human_int, stderr_console
from tg_collab.services import init_profile, make_http, profile_json, range_json


def register(app: typer.Typer) -> None:
    @app.command("init")
    def init_command(
        ctx: typer.Context,
        channel: str = typer.Argument(
            ..., help="Your channel: @name, t.me/name, or https://t.me/s/name."
        ),
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
        posts: int | None = typer.Option(
            None, "--posts", help="How many recent posts to fetch for metrics."
        ),
    ) -> None:
        """Profile your channel and derive the partner size range."""
        conn, settings = open_db_with_settings(ctx)
        try:
            http = make_http(settings)
            try:
                result = init_profile(conn, settings, http, channel, posts_limit=posts)
            finally:
                http.close()
        finally:
            conn.close()

        profile = result["profile"]
        target = result["range"]
        payload = {
            "profile": profile_json(profile),
            "range": range_json(target),
            "warnings": result["warnings"],
        }

        if json_output:
            emit_json(payload)
            return

        for warning in result["warnings"]:
            stderr_console.print(f"[yellow]warning:[/yellow] {warning}")

        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_row("Title", profile.title or "-")
        table.add_row("Username", f"@{profile.username}")
        table.add_row("Subscribers", human_int(profile.subscribers))
        table.add_row("Average views", human_int(profile.avg_views))
        table.add_row("Median views", human_int(profile.median_views))
        table.add_row("ERR", human_float(profile.err, 3))
        table.add_row("Posts/week", human_float(profile.posts_per_week, 2))
        table.add_row(
            "Target range",
            f"{human_int(target.low)} - {human_int(target.high)} subscribers "
            f"({target.min_mult}x - {target.max_mult}x)",
        )
        Console().print(Panel(table, title="Your channel", expand=False))
