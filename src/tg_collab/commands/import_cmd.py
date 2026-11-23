"""``tg-collab import`` - verify and store channels found by the user's AI agent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import typer

from tg_collab.commands.common import open_db_with_settings
from tg_collab.config import get_int
from tg_collab.enrichment import enrich_channel
from tg_collab.errors import ChannelNotFound, TgCollabError
from tg_collab.models import Candidate, Lead
from tg_collab.output import emit_json, stderr_console
from tg_collab.services import capture_invite_lead, make_http
from tg_collab.sources.base import ProviderContext
from tg_collab.sources.tg_public import extract_invite_links, resolve_username
from tg_collab.store import get_profile

MAX_IMPORT_BYTES = 1_000_000
TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "www.t.me", "www.telegram.me"})


@dataclass
class ImportPlan:
    items: list[dict[str, str]] = field(default_factory=list)
    leads: list[dict[str, str]] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)
    limited: int = 0


def _target_username(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty target")
    lowered = raw.lower()
    if lowered.startswith("tg://"):
        query = parse_qs(urlsplit(raw).query)
        domain = (query.get("domain") or [""])[0]
        if not domain:
            raise ValueError("tg:// URL has no domain")
        return resolve_username(domain)
    if lowered.startswith(("http://", "https://")):
        host = (urlsplit(raw).hostname or "").lower()
        if host not in TELEGRAM_HOSTS:
            raise ValueError("only t.me links are accepted")
    return resolve_username(raw)


def _add_entry(
    entry: Any, plan: ImportPlan, seen: set[str], seen_leads: set[str]
) -> None:
    if isinstance(entry, str):
        raw = entry
        why = ""
        url = ""
    elif isinstance(entry, dict):
        raw = str(entry.get("username") or entry.get("url") or entry.get("channel") or "")
        why = str(entry.get("why") or entry.get("reason") or "")
        url = str(entry.get("url") or "")
    else:
        plan.problems.append({"target": str(entry), "reason": "unsupported entry"})
        return
    invites = extract_invite_links(raw, url)
    for invite in invites:
        if invite not in seen_leads:
            seen_leads.add(invite)
            plan.leads.append({"url": invite, "why": why})
    try:
        username = _target_username(raw)
    except ValueError as exc:
        if not invites:
            plan.problems.append({"target": raw, "reason": str(exc)})
        return
    key = username.lower()
    if key in seen:
        return
    seen.add(key)
    plan.items.append({"username": username, "url": url, "why": why})


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _embedded_json(body: str) -> Any:
    """Pull one array/object out of agent prose that wraps its answer."""
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = body.find(opener), body.rfind(closer)
        if start == -1 or end <= start:
            continue
        try:
            return json.loads(body[start : end + 1])
        except ValueError:
            continue
    return None


def parse_payload(text: str) -> ImportPlan:
    plan = ImportPlan()
    seen: set[str] = set()
    seen_leads: set[str] = set()
    body = _strip_fence(text)
    if not body:
        return plan
    parsed: Any = None
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = _embedded_json(body)
    if parsed is None:
        for line in body.splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped:
                continue
            if stripped.startswith("{"):
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    plan.problems.append({"target": stripped, "reason": "invalid JSON line"})
                    continue
                _add_entry(entry, plan, seen, seen_leads)
            else:
                _add_entry(stripped, plan, seen, seen_leads)
        return plan
    if isinstance(parsed, dict):
        parsed = parsed.get("channels") or parsed.get("results") or [parsed]
    if not isinstance(parsed, list):
        plan.problems.append({"target": body[:80], "reason": "expected a JSON array"})
        return plan
    for entry in parsed:
        _add_entry(entry, plan, seen, seen_leads)
    return plan


def apply_limit(
    plan: ImportPlan,
    settings: dict[str, str],
    key: str = "agent_import_max",
    default: int = 50,
) -> ImportPlan:
    # ponytail: explicit invite links are one-off manual targets, so the bulk cap stays on usernames.
    limit = get_int(settings, key)
    limit = default if limit is None else max(limit, 0)
    if len(plan.items) > limit:
        plan.limited = len(plan.items) - limit
        plan.items = plan.items[:limit]
    return plan


def collect_targets(targets: list[str], settings: dict[str, str]) -> ImportPlan:
    plan = ImportPlan()
    seen: set[str] = set()
    seen_leads: set[str] = set()
    for target in targets:
        raw = target.strip()
        if not raw:
            continue
        path = Path(raw)
        try:
            is_file = path.is_file()
        except OSError:
            is_file = False
        if is_file:
            if path.stat().st_size > MAX_IMPORT_BYTES:
                plan.problems.append({"target": raw, "reason": "file is larger than 1 MB"})
                continue
            nested = parse_payload(path.read_text(encoding="utf-8", errors="replace"))
            for item in nested.items:
                if item["username"].lower() not in seen:
                    seen.add(item["username"].lower())
                    plan.items.append(item)
            for lead in nested.leads:
                if lead["url"] not in seen_leads:
                    seen_leads.add(lead["url"])
                    plan.leads.append(lead)
            plan.problems.extend(nested.problems)
            continue
        for token in re.split(r"[,;\n]+", raw):
            _add_entry(token, plan, seen, seen_leads)
    return apply_limit(plan, settings)


def run_import(conn, settings, http, profile, plan: ImportPlan) -> dict[str, Any]:
    ctx = ProviderContext(
        settings=settings, http=http, profile=profile, store=conn, profile_id=profile.id
    )
    leads: list[dict[str, Any]] = []
    for item in plan.leads:
        stored = capture_invite_lead(
            conn,
            http,
            Lead(
                url=item["url"],
                source="import",
                note=item["why"] or "Imported as an invite link",
            ),
            profile_id=profile.id,
        )
        leads.append(
            {
                "url": stored.url,
                "status": "accepted",
                "reason": "",
                "title": stored.title or "",
                "subscribers": stored.subscribers,
            }
        )
    accepted = len(leads)
    results: list[dict[str, str]] = []
    for item in plan.items:
        meta = {"why": item["why"], "url": item["url"]}
        candidate = Candidate(username=item["username"], source="agent_import", meta=meta)
        try:
            enrich_channel(candidate, ctx)
        except ChannelNotFound as exc:
            results.append(
                {"username": item["username"], "status": "rejected", "reason": exc.message}
            )
        except Exception as exc:  # noqa: BLE001 - one bad target must not abort the import
            results.append(
                {
                    "username": item["username"],
                    "status": "rejected",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            accepted += 1
            results.append({"username": item["username"], "status": "accepted", "reason": ""})
    total = len(plan.items) + len(plan.leads)
    return {
        "accepted": accepted,
        "rejected": total - accepted,
        "total": total,
        "limited": plan.limited,
        "results": results,
        "leads": leads,
        "problems": plan.problems,
    }


def register(app: typer.Typer) -> None:
    @app.command("import")
    def import_command(
        ctx: typer.Context,
        targets: list[str] = typer.Argument(..., help="File, @a,@b, or t.me URL."),
        json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    ) -> None:
        """Verify agent-found channels through t.me and store the real ones."""
        conn, settings = open_db_with_settings(ctx)
        try:
            profile = get_profile(conn)
            if profile is None:
                raise TgCollabError("no profile yet - run `tg-collab init @your_channel` first")
            plan = collect_targets(targets, settings)
            http = make_http(settings)
            try:
                result = run_import(conn, settings, http, profile, plan)
            finally:
                http.close()
        finally:
            conn.close()

        payload = {
            "accepted": result["accepted"],
            "rejected": result["rejected"],
            "total": result["total"],
            "limited": result["limited"],
            "results": result["results"],
            "leads": result["leads"],
            "problems": result["problems"],
        }
        if json_output:
            emit_json(payload)
        else:
            typer.echo(f"accepted {payload['accepted']}, rejected {payload['rejected']}")
            for row in payload["leads"]:
                typer.echo(f"lead {row['url']}")
            for row in payload["results"]:
                if row["status"] == "rejected":
                    stderr_console.print(f"[yellow]skip[/yellow] @{row['username']}: {row['reason']}")
            for problem in payload["problems"]:
                stderr_console.print(f"[yellow]skip[/yellow] {problem['target']}: {problem['reason']}")
        if result["accepted"] == 0:
            raise TgCollabError("no targets were confirmed; see the reasons above")
