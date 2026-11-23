"""``tg-collab niche`` - show or set the niche profile used for queries and ranking."""

from __future__ import annotations

import typer

from tg_collab.commands.common import open_db_with_settings
from tg_collab.errors import TgCollabError, UsageError
from tg_collab.niche import (
    TERM_LIMIT,
    clear_niche,
    generate_niche,
    load_niche,
    make_manual_niche,
    save_niche,
)
from tg_collab.output import emit_json, stderr_console
from tg_collab.similarity import niche_tags, rescore_channels
from tg_collab.store import get_profile


def _payload(profile) -> dict:
    stored = load_niche(profile)
    terms, hashtags, domains = niche_tags(profile)
    payload = {
        "username": profile.username,
        "source": stored.source if stored is not None else "deterministic",
        "generated_at": stored.generated_at if stored is not None else "",
        "terms": terms,
        "hashtags": hashtags,
        "domains": domains,
        "stopwords": list(stored.stopwords) if stored is not None else [],
        "summary": stored.summary if stored is not None else "",
    }
    return payload


def _print_human(payload: dict) -> None:
    typer.echo(f"Source: {payload['source']}")
    typer.echo(f"Terms: {', '.join(payload['terms']) or '-'}")
    if payload["hashtags"]:
        typer.echo("Hashtags: " + ", ".join(f"#{tag}" for tag in payload["hashtags"]))
    if payload["domains"]:
        typer.echo("Domains: " + ", ".join(payload["domains"]))
    if payload["summary"]:
        typer.echo(f"Summary: {payload['summary']}")


def register(app: typer.Typer) -> None:
    @app.command("niche")
    def niche_command(
        ctx: typer.Context,
        refresh: bool = typer.Option(
            False, "--refresh", help="Ask the local agent for a fresh niche profile."
        ),
        set_terms: str | None = typer.Option(
            None, "--set", help='Store manual terms, for example "github,ии,инструменты".'
        ),
        clear: bool = typer.Option(False, "--clear", help="Remove the stored profile."),
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    ) -> None:
        """Show the effective niche profile, ask the agent, or set terms by hand."""
        if sum((bool(refresh), set_terms is not None, bool(clear))) > 1:
            raise UsageError("choose one of --refresh, --set, or --clear")
        conn, settings = open_db_with_settings(ctx)
        try:
            profile = get_profile(conn)
            if profile is None:
                raise TgCollabError("no profile yet - run `tg-collab init @your_channel` first")
            if clear:
                clear_niche(conn)
            elif set_terms is not None:
                terms = [part.strip() for part in set_terms.split(",") if part.strip()]
                if not 1 <= len(terms) <= TERM_LIMIT:
                    raise UsageError(f"--set needs 1-{TERM_LIMIT} comma-separated terms")
                niche = make_manual_niche(terms, previous=load_niche(profile))
                if niche is None:
                    raise UsageError(
                        "--set terms cannot contain URLs, @handles, or hashtag signs"
                    )
                save_niche(conn, niche)
            elif refresh:
                generated, warning = generate_niche(settings, profile)
                if generated is None:
                    raise TgCollabError(warning or "the agent did not return a niche profile")
                save_niche(conn, generated)
            stored_profile = get_profile(conn)
            assert stored_profile is not None
            rescore_channels(conn, stored_profile, settings, profile_id=stored_profile.id)
            payload = _payload(stored_profile)
        finally:
            conn.close()

        if json_output:
            emit_json(payload)
            return
        _print_human(payload)
        if refresh:
            stderr_console.print("[dim]stored the agent niche profile[/dim]")
