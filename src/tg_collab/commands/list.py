"""``tg-collab list`` - channels with dynamic tabs, filters, and agent context."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from tg_collab.commands.common import open_db_with_settings
from tg_collab.errors import TgCollabError, UsageError
from tg_collab.output import emit_json
from tg_collab.reports import ACTIVITY_FILTERS, LANG_FILTERS, build_row, build_rows
from tg_collab.store import get_channel, get_posts, get_profile, latest_draft

TABS = ("all", "suitable", "growing", "hidden")


def register(app: typer.Typer) -> None:
    @app.command("list")
    def list_command(
        ctx: typer.Context,
        channel: str | None = typer.Argument(None, help="Show one channel with draft context."),
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
        tab: str = typer.Option("all", "--tab", help="all|suitable|growing|hidden"),
        status: str | None = typer.Option(None, "--status"),
        contact: str | None = typer.Option(None, "--contact"),
        source: str | None = typer.Option(None, "--source"),
        flag: str | None = typer.Option(None, "--flag"),
        q: str | None = typer.Option(None, "--q", help="Search username, title, description."),
        lang: str | None = typer.Option(
            None,
            "--lang",
            help="all|cyrillic|latin|cjk. Default keeps the owner's language and English.",
        ),
        activity: str = typer.Option(
            "active",
            "--activity",
            help="active|all. Default hides channels inactive for flag_inactive_days.",
        ),
        limit: int | None = typer.Option(None, "--limit"),
    ) -> None:
        """List channels, classified against the current profile."""
        if tab not in TABS:
            raise UsageError(f"--tab must be one of: {', '.join(TABS)}")
        if lang is not None and lang not in LANG_FILTERS:
            raise UsageError(f"--lang must be one of: {', '.join(LANG_FILTERS)}")
        if activity not in ACTIVITY_FILTERS:
            raise UsageError(f"--activity must be one of: {', '.join(ACTIVITY_FILTERS)}")
        conn, settings = open_db_with_settings(ctx)
        try:
            profile = get_profile(conn)
            if profile is None:
                raise TgCollabError("no profile yet - run `tg-collab init @your_channel` first")

            if channel:
                stored = get_channel(conn, username=channel)
                if stored is None:
                    raise TgCollabError(f"channel not found: @{channel.lstrip('@')}")
                row = build_row(conn, profile, stored, settings)
                draft = latest_draft(conn, stored.id)
                posts = get_posts(conn, stored.id, limit=5)
                payload = {
                    "channel": {
                        **row,
                        "description": stored.description,
                        "notes": stored.notes,
                        "latest_draft": draft.text if draft is not None else None,
                        "posts": [
                            {
                                "message_id": post.message_id,
                                "posted_at": post.posted_at,
                                "views": post.views,
                                "text": post.text,
                            }
                            for post in posts
                        ],
                    }
                }
                if json_output:
                    emit_json(payload)
                else:
                    Console().print_json(data=payload)
                return

            rows = build_rows(
                conn,
                profile,
                settings,
                tab=tab,
                status=status,
                contact=contact,
                source=source,
                flag=flag,
                q=q,
                lang=lang,
                activity=activity,
                limit=limit,
            )
        finally:
            conn.close()

        if json_output:
            emit_json({"channels": rows, "count": len(rows)})
            return

        table = Table(title=f"Channels ({len(rows)})", box=None)
        columns = (
            "username", "subscribers", "avg_views", "status", "tab", "lang", "contact", "flags",
        )
        for column in columns:
            table.add_column(column)
        for row in rows:
            table.add_row(
                f"@{row['username']}",
                "-" if row["subscribers"] is None else str(row["subscribers"]),
                "-" if row["avg_views"] is None else str(row["avg_views"]),
                row["status"],
                row["tab"],
                row["lang"] or "-",
                row["contact_status"],
                ", ".join(row["flags"]),
            )
        Console().print(table)
