"""``tg-collab export`` - write the outreach digest as Markdown or CSV."""

from __future__ import annotations

from pathlib import Path

import typer

from tg_collab.commands.common import open_db_with_settings
from tg_collab.config import get_language
from tg_collab.errors import UsageError
from tg_collab.output import emit_json
from tg_collab.reports import (
    ACTIVITY_FILTERS,
    LANG_FILTERS,
    build_rows,
    export_csv,
    export_markdown,
)
from tg_collab.store import get_profile


def register(app: typer.Typer) -> None:
    @app.command("export")
    def export_command(
        ctx: typer.Context,
        format: str = typer.Option("md", "--format", help="md|csv"),
        out: Path | None = typer.Option(None, "--out", help="Output path (default: outreach.<format>)."),
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
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    ) -> None:
        """Export all non-ignored channels grouped by status."""
        if format not in ("md", "csv"):
            raise UsageError("--format must be md or csv")
        if lang is not None and lang not in LANG_FILTERS:
            raise UsageError(f"--lang must be one of: {', '.join(LANG_FILTERS)}")
        if activity not in ACTIVITY_FILTERS:
            raise UsageError(f"--activity must be one of: {', '.join(ACTIVITY_FILTERS)}")
        conn, settings = open_db_with_settings(ctx)
        try:
            profile = get_profile(conn)
            rows = build_rows(
                conn, profile, settings, tab="all", lang=lang, activity=activity
            )
            rows = [row for row in rows if row["status"] != "ignored"]
            content = (
                export_markdown(rows, get_language(settings))
                if format == "md"
                else export_csv(rows)
            )
        finally:
            conn.close()

        path = Path(out) if out is not None else Path(f"outreach.{format}")
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        payload = {"path": str(path), "format": format, "count": len(rows)}
        if json_output:
            emit_json(payload)
        else:
            typer.echo(f"wrote {len(rows)} channels to {path}")
