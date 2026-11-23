"""``tg-collab find`` - discover peer channels from the public graph."""

from __future__ import annotations

from functools import partial
import threading

import typer
from rich.console import Console
from rich.table import Table

from tg_collab.commands.common import open_db_with_settings
from tg_collab.discovery import SOURCE_KEYS, DiscoveryRun
from tg_collab.enrichment import enrich_channel
from tg_collab.errors import TgCollabError
from tg_collab.niche import ensure_niche, load_niche
from tg_collab.output import emit_json, stderr_console
from tg_collab.services import make_http
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.registry import build_discovery_providers
from tg_collab.sources.tg_public import TelegramPublicClient
from tg_collab.store import get_profile

PROVIDER_KEYS = SOURCE_KEYS


def register(app: typer.Typer) -> None:
    @app.command("find")
    def find_command(
        ctx: typer.Context,
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
        depth: int | None = typer.Option(
            None, "--depth", help="Override find_max_depth (0 = direct neighbours only)."
        ),
        max_channels: int | None = typer.Option(
            None, "--max-channels", help="Override find_max_channels for this run."
        ),
    ) -> None:
        """Discover peer channels from every enabled source."""
        conn, settings = open_db_with_settings(ctx)
        try:
            profile = get_profile(conn)
            if profile is None:
                raise TgCollabError("no profile yet - run `tg-collab init @your_channel` first")
            if depth is not None:
                settings["find_max_depth"] = str(depth)
            if max_channels is not None:
                settings["find_max_channels"] = str(max_channels)

            http = make_http(settings)
            try:
                niche_warnings: list[str] = []
                if not (profile.niche_json or "").strip():
                    stderr_console.print("[dim]asking the local agent for the niche profile[/dim]")
                    profile, niche_warnings = ensure_niche(
                        conn, settings, profile, profile_id=profile.id
                    )
                    stderr_console.print(
                        "[dim]niche profile ready[/dim]"
                        if load_niche(profile)
                        else "[dim]using the deterministic niche terms[/dim]"
                    )
                provider_ctx = ProviderContext(
                    settings=settings, http=http, profile=profile, store=conn,
                    profile_id=profile.id, cancel=threading.Event(),
                )
                provider_ctx.warnings.extend(niche_warnings)
                providers = build_discovery_providers(settings, http, provider_ctx.warnings)
                run = DiscoveryRun(
                    settings=settings,
                    providers=providers,
                    public_client=TelegramPublicClient(http),
                    enrich=partial(enrich_channel, ctx=provider_ctx),
                    store=conn,
                    ctx=provider_ctx,
                )

                def progress(done: int, total: int, message: str) -> None:
                    stderr_console.print(f"[dim]{done}/{total} {message}[/dim]")

                stats = run.run(profile.username, progress=None if json_output else progress)
            finally:
                http.close()
        finally:
            conn.close()

        payload = {
            "added": stats.added,
            "updated": stats.updated,
            "skipped_fresh": stats.skipped_fresh,
            "seeds": stats.seeds,
            "errors": stats.errors,
            "warnings": stats.warnings,
            "providers": {key: stats.providers.get(key, 0) for key in PROVIDER_KEYS},
            "sources_ok": stats.sources_ok,
            "source_status": stats.source_status,
            "source_remedy": stats.source_remedy,
        }
        if json_output:
            emit_json(payload)
        if stats.sources_ok == 0:
            raise TgCollabError("every enabled source failed; see errors")
        if json_output:
            return

        table = Table(title="Discovery", show_header=True, box=None)
        table.add_row("Added", str(stats.added))
        table.add_row("Updated", str(stats.updated))
        table.add_row("Skipped (fresh)", str(stats.skipped_fresh))
        table.add_row("Errors", str(len(stats.errors)))
        for name, count in payload["providers"].items():
            table.add_row(f"Source {name}", str(count))
        Console().print(table)
        for warning in stats.warnings:
            stderr_console.print(f"[yellow]warning:[/yellow] {warning}")
        for error in stats.errors:
            stderr_console.print(f"[red]error:[/red] {error['username']}: {error['error']}")
