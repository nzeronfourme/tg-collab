"""``tg-collab explain`` - why is this channel in or not in my list?"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import typer
from rich.console import Console

from tg_collab.commands.common import open_db_with_settings
from tg_collab.config import get_int
from tg_collab.errors import TgCollabError
from tg_collab.language import UNKNOWN
from tg_collab.models import Channel, Lead, Profile
from tg_collab.output import emit_json
from tg_collab.profile import TargetRange
from tg_collab.reports import build_row, target_range, visible_languages
from tg_collab.services import make_http
from tg_collab.sources.tg_public import (
    INVITE_HASH_RE,
    TelegramPublicClient,
    resolve_username,
)
from tg_collab.store import get_channel, get_lead_by_url, get_profile

# ``score`` is similarity/100 * size_fit: under 5 the match rests on
# incidental words, so the channel is stored but ranks under almost anything.
VISIBILITY_SCORE_FLOOR = 5.0

INVITE_ONLY_REASON = (
    "This is an invite link: it cannot be verified or enriched without joining, "
    "so it stays a manual lead and joining stays out of scope."
)


def parse_target(raw: str) -> tuple[str, str]:
    """Return ``("username", name)`` or ``("invite", canonical https URL)``."""
    value = (raw or "").strip()
    if not value:
        raise TgCollabError("empty target - pass @name, a t.me link, or t.me/+invite")
    invite = _invite_from(value)
    if invite is not None:
        return "invite", f"https://t.me/{invite}"
    try:
        return "username", resolve_username(value)
    except ValueError as exc:
        raise TgCollabError(f"invalid target {raw!r}: {exc}") from None


def _invite_from(value: str) -> str | None:
    stripped = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    stripped = re.sub(
        r"^(?:www\.)?(?:t\.me|telegram\.me)/", "", stripped, flags=re.IGNORECASE
    )
    stripped = stripped.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not stripped.startswith("+"):
        return None
    token = stripped[1:]
    if not INVITE_HASH_RE.fullmatch(token):
        raise TgCollabError(f"invalid invite link {value!r}: expected t.me/+<hash>")
    return f"+{token}"


def _range_payload(target: TargetRange | None) -> dict[str, int] | None:
    if target is None:
        return None
    return {"low": target.low, "high": target.high}


def _stored_lead(conn: sqlite3.Connection, invite_url: str) -> Lead | None:
    """The lead row for an invite, or None; pre-v3 databases have no leads table."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'"
    ).fetchone()
    if table is None:
        return None
    try:
        return get_lead_by_url(conn, invite_url)
    except sqlite3.OperationalError:
        return None


def _lead_payload(lead: Lead) -> dict[str, Any]:
    return {
        "url": lead.url,
        "title": lead.title,
        "description": lead.description,
        "subscribers": lead.subscribers,
        "source": lead.source,
        "note": lead.note,
        "created_at": lead.created_at,
    }


def _hidden_by_language(channel: Channel, profile: Profile) -> bool:
    allowed = visible_languages(profile)
    if allowed is None:
        return False
    return (channel.lang or UNKNOWN) not in allowed


def _language_reason(username: str, channel: Channel, profile: Profile) -> str:
    lang = channel.lang or UNKNOWN
    lang_label = "CJK" if lang == "cjk" else lang
    owner = visible_languages(profile) or frozenset()
    owner_code = sorted(owner - {UNKNOWN, "latin"})
    owner_label = owner_code[0] if owner_code else "other"
    return (
        f"@{username} is stored but its language is {lang_label}, outside the "
        f"default set ({owner_label} plus English). Pass `--lang {lang}` to see it."
    )


def _size_text(row: dict[str, Any]) -> str:
    if row["subscribers"] is not None:
        return f"{row['subscribers']} subscribers"
    if row["avg_views"] is not None:
        return f"{row['avg_views']} average views"
    return "unknown size"


def _range_reason(username: str, row: dict[str, Any]) -> str:
    kind = row["range_reason"]
    # ponytail: only the three classify() kinds exist; unknown_size covers the rest.
    if kind == "unknown_size":
        return (
            f"@{username} is stored but its size is unknown, so it waits "
            "under 'Grow into it'."
        )
    side = "small" if kind == "too_small" else "big"
    return (
        f"@{username} is stored but too {side} for your current range "
        f"({_size_text(row)}), so it waits under 'Grow into it'."
    )


def _channel_verdict(
    stored: Channel,
    row: dict[str, Any],
    low: int,
    high: int,
    profile: Profile,
    settings: dict[str, str],
) -> tuple[str, str]:
    """Verdict precedence: ignored, then activity, then language, then size, then score.

    An explicit user decision (ignored) outranks every automatic filter. The
    activity window comes next because a stale channel is hidden by default
    whatever its size or language; then the default language set, so a stored
    CJK row is not reported as present while the list hides it.
    """
    username = stored.username
    if stored.status == "ignored":
        return (
            "hidden",
            f"@{username} is stored but marked ignored, so the list keeps it "
            "on the Hidden tab.",
        )
    if "inactive" in row["flags"]:
        days = get_int(settings, "flag_inactive_days") or 14
        last = (stored.last_post_at or "")[:10] or "unknown"
        score = stored.score
        return (
            "inactive",
            f"@{username} is stored (tab {row['tab']}, score "
            f"{f'{score:.2f}' if score is not None else '-'}), but its last "
            f"post is {last}, older than the {days}-day activity window, so the "
            "default list hides it. Pass `--activity all` or tick \"Show "
            "inactive\" in the Web UI to include it.",
        )
    if _hidden_by_language(stored, profile):
        return "hidden_language", _language_reason(username, stored, profile)
    if row["range_reason"] != "in_range":
        return "out_of_range", _range_reason(username, row)
    score = stored.score
    if score is None:
        return (
            "stored_low",
            f"@{username} is stored inside your range but has no score yet; "
            "re-run `tg-collab find` to rank it.",
        )
    if score < VISIBILITY_SCORE_FLOOR:
        return (
            "stored_low",
            f"@{username} is stored inside your range but scores {score:.2f}, "
            f"under the {VISIBILITY_SCORE_FLOOR:g} visibility floor, so it "
            "ranks below stronger matches.",
        )
    return (
        "present",
        f"@{username} is in your list: score {score:.2f}, inside the "
        f"{low}..{high} subscriber range ({row['tab']}).",
    )


def explain_channel(
    conn: sqlite3.Connection,
    profile: Profile,
    settings: dict[str, str],
    username: str,
    target: TargetRange,
) -> dict[str, Any]:
    stored = get_channel(conn, username=username)
    if stored is None:
        payload: dict[str, Any] = {
            "in_db": False,
            "verdict": "absent",
            "reason": (
                f"@{username} is not in your database; run `tg-collab find` or "
                "`tg-collab import` to add it."
            ),
            "channel": None,
        }
    else:
        row = build_row(conn, profile, stored, settings)
        verdict, reason = _channel_verdict(
            stored, row, target.low, target.high, profile, settings
        )
        payload = {
            "in_db": True,
            "verdict": verdict,
            "reason": reason,
            "channel": row,
        }
    return {
        "target": username,
        "in_db": payload["in_db"],
        "verdict": payload["verdict"],
        "reason": payload["reason"],
        "channel": payload["channel"],
        "range": _range_payload(target),
        "invite": False,
    }


def explain_invite(
    conn: sqlite3.Connection,
    invite_url: str,
    target: TargetRange | None,
) -> dict[str, Any]:
    lead = _stored_lead(conn, invite_url)
    payload: dict[str, Any] = {
        "target": invite_url,
        "in_db": lead is not None,
        "verdict": "invite_only",
        "reason": INVITE_ONLY_REASON,
        "channel": None,
        "range": _range_payload(target),
        "invite": True,
    }
    if lead is not None:
        payload["lead"] = _lead_payload(lead)
    return payload


def check_reachability(
    settings: dict[str, str], kind: str, value: str
) -> dict[str, Any]:
    if kind == "invite":
        return {"exists": False, "error": "invite links cannot be checked without joining"}
    http = make_http(settings)
    try:
        exists = TelegramPublicClient(http).channel_exists(value)
    except Exception as exc:  # noqa: BLE001 - a failed probe is reported, not fatal
        return {"exists": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        http.close()
    return {"exists": exists, "error": None}


def _print_human(payload: dict[str, Any]) -> None:
    console = Console()
    label = payload["target"] if payload["invite"] else f"@{payload['target']}"
    console.print(f"[bold]{label}[/bold] -> [bold]{payload['verdict']}[/bold]")
    console.print(payload["reason"])
    channel = payload["channel"]
    if channel is not None:
        console.print(
            f"score {channel['score']} | similarity {channel['similarity']} | "
            f"size_fit {channel['size_fit']} | tab {channel['tab']} | "
            f"range {channel['range_reason']} | "
            f"flags {', '.join(channel['flags']) or '-'}"
        )
    target = payload["range"]
    if target is not None:
        console.print(f"target range {target['low']}..{target['high']} subscribers")
    check = payload.get("check")
    if check is not None:
        suffix = f" ({check['error']})" if check["error"] else ""
        console.print(f"t.me exists: {check['exists']}{suffix}")
    console.print(f"in_db: {payload['in_db']}")


def register(app: typer.Typer) -> None:
    @app.command("explain")
    def explain_command(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="@name, t.me/name, or t.me/+invite."),
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
        check: bool = typer.Option(
            False, "--check", help="Probe t.me once for the channel (network call)."
        ),
    ) -> None:
        """Explain why a channel is in or not in your list."""
        kind, value = parse_target(target)
        conn, settings = open_db_with_settings(ctx)
        try:
            profile = get_profile(conn)
            if kind == "username" and profile is None:
                raise TgCollabError(
                    "no profile yet - run `tg-collab init @your_channel` first"
                )
            target_range_value = target_range(profile, settings)
            if kind == "invite":
                payload = explain_invite(conn, value, target_range_value)
            else:
                payload = explain_channel(
                    conn, profile, settings, value, target_range_value
                )
            if check:
                payload["check"] = check_reachability(settings, kind, value)
        finally:
            conn.close()

        if json_output:
            emit_json(payload)
        else:
            _print_human(payload)


