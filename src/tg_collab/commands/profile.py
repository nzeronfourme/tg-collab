"""``tg-collab profile`` - manage the channels this database tracks."""

from __future__ import annotations

import sqlite3

import typer
from rich.console import Console
from rich.table import Table

from tg_collab.commands.common import guard, open_db
from tg_collab.errors import TgCollabError
from tg_collab.models import Profile
from tg_collab.output import emit_json
from tg_collab.sources.tg_public import resolve_username
from tg_collab.store import (
    active_profile_id,
    create_profile,
    delete_profile,
    list_profiles,
    set_active_profile,
)

profile_app = typer.Typer(
    help="Manage profiles: each is one channel with its own outreach list."
)


def _find(conn: sqlite3.Connection, target: str) -> Profile:
    raw = str(target or "").strip()
    profiles = list_profiles(conn)
    if raw.isdigit():
        for profile in profiles:
            if profile.id == int(raw):
                return profile
    else:
        name = raw.lstrip("@").lower()
        for profile in profiles:
            if profile.username.lower() == name:
                return profile
    raise TgCollabError(f"profile not found: {raw or '(empty)'}")


def _entry(profile: Profile, active: int | None) -> dict:
    return {
        "id": profile.id,
        "username": profile.username,
        "subscribers": profile.subscribers,
        "active": profile.id == active,
    }


def _emit(payload: dict, json_output: bool, human: list[str]) -> None:
    if json_output:
        emit_json(payload)
        return
    for line in human:
        typer.echo(line)


@profile_app.command("list")
@guard
def list_command(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List the stored profiles, with the active one marked."""
    conn = open_db(ctx)
    try:
        active = active_profile_id(conn)
        profiles = [_entry(profile, active) for profile in list_profiles(conn)]
    finally:
        conn.close()
    if json_output:
        emit_json({"profiles": profiles, "active": active})
        return
    table = Table(title=f"Profiles ({len(profiles)})", box=None)
    for column in ("id", "username", "subscribers", "active"):
        table.add_column(column)
    for profile in profiles:
        table.add_row(
            str(profile["id"]),
            f"@{profile['username']}",
            "-" if profile["subscribers"] is None else str(profile["subscribers"]),
            "*" if profile["active"] else "",
        )
    Console().print(table)


@profile_app.command("add")
@guard
def add_command(
    ctx: typer.Context,
    channel: str = typer.Argument(..., help="Channel username, e.g. @alpha."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Add a profile; the first profile becomes active."""
    try:
        username = resolve_username(channel)
    except ValueError as exc:
        raise TgCollabError(f"invalid channel {channel!r}: {exc}") from None
    conn = open_db(ctx)
    try:
        try:
            created = create_profile(conn, username)
        except ValueError as exc:
            raise TgCollabError(str(exc)) from None
        active = active_profile_id(conn)
        payload = _entry(created, active)
    finally:
        conn.close()
    _emit(
        payload,
        json_output,
        [f"added @{payload['username']} (id {payload['id']})"],
    )


@profile_app.command("use")
@guard
def use_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Profile username or numeric id."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Make one profile active for the CLI, the agent, and the Web UI."""
    conn = open_db(ctx)
    try:
        profile = _find(conn, target)
        try:
            set_active_profile(conn, int(profile.id or 0))
        except LookupError as exc:
            raise TgCollabError(str(exc)) from None
        payload = _entry(profile, profile.id)
    finally:
        conn.close()
    _emit(payload, json_output, [f"active profile: @{payload['username']}"])


@profile_app.command("remove")
@guard
def remove_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Profile username or numeric id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Delete a profile with its channels, notes, drafts, leads, and jobs."""
    conn = open_db(ctx)
    try:
        profile = _find(conn, target)
        if not yes:
            typer.confirm(
                f"Delete profile @{profile.username} and its channels, notes, and drafts?",
                abort=True,
            )
        try:
            delete_profile(conn, int(profile.id or 0))
        except (ValueError, LookupError) as exc:
            raise TgCollabError(str(exc)) from None
        active = active_profile_id(conn)
        payload = {
            "removed": {"id": profile.id, "username": profile.username},
            "active": active,
        }
    finally:
        conn.close()
    _emit(payload, json_output, [f"removed @{payload['removed']['username']}"])


def register(app: typer.Typer) -> None:
    app.add_typer(profile_app, name="profile")
