"""Automatic local-agent discovery: one or more agent runs, one merged source.

Every configured command gets its own :class:`AgentRunner` and its own thread.
The whole batch shares one wall-clock deadline (``agent_search_timeout_sec``),
one candidate cap (``agent_search_max``) and one published status: extra agents
never turn into extra ``SOURCE_KEYS`` or extra statuses.

# allow: SIZE_OK - one batch state machine (commands, threads, deadline, merge);
# splitting it would scatter the lifecycle across modules with no second caller.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tg_collab.agent_runner import (
    AgentError,
    AgentRunner,
    AgentSpec,
    _split_command,
    resolve_agent_command,
)
from tg_collab.commands.agent_prompt import build_search_task
from tg_collab.commands.import_cmd import parse_payload
from tg_collab.config import get_bool, get_float, get_int, get_language, get_setting, proxy_env
from tg_collab.models import Candidate
from tg_collab.reports import target_range
from tg_collab.similarity import niche_tags
from tg_collab.sources.base import ProviderContext

#: Output above this size is rejected after the pipe is read. The cap is a
#: sanity gate on nonsense output, not a memory guard: ``communicate`` buffers
#: the child's stdout, so the run timeout is what bounds memory.
MAX_STDOUT_BYTES = 1_000_000
DEFAULT_MAX = 30
DEFAULT_TIMEOUT_SEC = 600.0
DEFAULT_CONCURRENCY = 2
MAX_CONCURRENCY = 3
#: Extra wait after cancelling a timed-out run, so the child gets killed first.
CANCEL_GRACE_SEC = 5.0
#: How often a queued agent re-checks the cancel flag while waiting for a slot.
SEMAPHORE_POLL_SEC = 0.05
#: How long one join slice waits before re-checking the run's cancel event.
JOIN_SLICE_SEC = 0.1


def _default_runner_factory(spec: AgentSpec) -> AgentRunner:
    return AgentRunner(spec)


def _executable_exists(executable: str) -> bool:
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return Path(executable).is_file()
    return shutil.which(executable) is not None


@dataclass
class _AgentJob:
    """One configured command and what its run produced."""

    index: int
    command: str
    status: str = "pending"
    reason: str = ""
    output: str | None = None
    error: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    thread: threading.Thread | None = None


class AgentSearchProvider:
    name = "agent_search"

    def __init__(
        self,
        settings: dict[str, str],
        http: Any = None,
        runner_factory: Callable[[AgentSpec], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.http = http
        self.status = "idle"
        self.warnings: list[str] = []
        self._runner_factory = runner_factory or _default_runner_factory
        self._started = False
        self._jobs: list[_AgentJob] = []
        self._cancel = threading.Event()
        self._deadline: float | None = None
        self._sem: threading.Semaphore | None = None
        self._candidates: list[Candidate] | None = None

    def enabled(self, settings: dict[str, str]) -> bool:
        return get_bool(settings, "sources_agent_search")

    def _timeout(self) -> float:
        value = get_float(self.settings, "agent_search_timeout_sec")
        return DEFAULT_TIMEOUT_SEC if value is None or value <= 0 else value

    def _concurrency(self) -> int:
        value = get_int(self.settings, "agent_search_concurrency")
        if value is None:
            value = DEFAULT_CONCURRENCY
        return max(1, min(MAX_CONCURRENCY, value))

    def _configured_commands(self, ctx: ProviderContext | None) -> list[str]:
        """Split ``agent_commands`` by line, or fall back to ``agent_command``.

        Blank lines are skipped. A repeated command is collapsed to its first
        occurrence and reported once, so the 1-based agent index always matches
        the deduped order the batch actually runs.
        """
        raw = get_setting(self.settings, "agent_commands")
        commands: list[str] = []
        duplicates = 0
        for line in (raw or "").splitlines():
            command = line.strip()
            if not command:
                continue
            if command in commands:
                duplicates += 1
                continue
            commands.append(command)
        if not commands:
            fallback = get_setting(self.settings, "agent_command").strip()
            if fallback:
                commands.append(fallback)
            else:
                try:
                    commands.append(resolve_agent_command())
                except AgentError as exc:
                    self._warn(ctx, f"agent_search: {exc}")
        if duplicates:
            plural = "line" if duplicates == 1 else "lines"
            self._warn(ctx, f"agent_search: ignored {duplicates} duplicate agent command {plural}")
        return commands

    def _warn(self, ctx: ProviderContext | None, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        if ctx is not None and message not in ctx.warnings:
            ctx.warnings.append(message)

    def _publish(self, ctx: ProviderContext | None) -> None:
        statuses = getattr(ctx, "statuses", None) if ctx is not None else None
        if isinstance(statuses, dict):
            statuses[self.name] = self.status

    def _stop(self, ctx: ProviderContext | None, status: str) -> list[Candidate]:
        self.status = status
        self._candidates = []
        self._publish(ctx)
        return []

    @staticmethod
    def _invalid_warning(problems: list[dict[str, str]]) -> str:
        """Name the dropped entries while keeping the verified candidates."""
        reasons: list[str] = []
        for problem in problems:
            reason = str(problem.get("reason") or "invalid entry")
            if reason not in reasons:
                reasons.append(reason)
        count = len(problems)
        plural = "entry" if count == 1 else "entries"
        return (
            f"skipped {count} invalid {plural}, kept the valid ones "
            f"({', '.join(reasons[:3])})"
        )

    def start(self, ctx: ProviderContext | None) -> None:
        if self._started:
            return
        self._started = True
        profile = ctx.profile if ctx is not None else None
        if profile is None:
            self._warn(ctx, "agent_search: no profile to describe")
            self._stop(ctx, "error")
            return

        commands = self._configured_commands(ctx)
        if not commands:
            self._warn(ctx, "agent_search: agent_command is empty; set it in Settings")
            self._stop(ctx, "disabled")
            return

        jobs: list[_AgentJob] = []
        for index, command in enumerate(commands, start=1):
            try:
                argv = _split_command(command)
            except (AgentError, ValueError) as exc:
                # shlex raises ValueError on unbalanced quotes; skip just that line.
                self._warn(ctx, f"agent_search: agent {index} failed: {exc}")
                continue
            executable = argv[0]
            if not _executable_exists(executable):
                self._warn(
                    ctx,
                    f"agent_search: agent {index}: {executable!r} was not found; "
                    "set agent_commands or agent_command in Settings",
                )
                continue
            jobs.append(_AgentJob(index=index, command=command))
        if not jobs:
            self._warn(ctx, "agent_search: no configured agent executable is available")
            self._stop(ctx, "disabled")
            return

        target = target_range(profile, self.settings)
        if target is None:
            self._warn(ctx, "agent_search: the profile has no target range")
            self._stop(ctx, "error")
            return

        terms, tags, domains = niche_tags(profile)
        prompt = build_search_task(
            profile, target, terms, get_language(self.settings),
            hashtags=tags, domains=domains,
            region=get_setting(self.settings, "search_region"),
        )
        self._jobs = jobs
        self._deadline = time.monotonic() + self._timeout()
        self._sem = threading.Semaphore(self._concurrency())
        for job in jobs:
            job.thread = threading.Thread(
                target=self._work,
                args=(job, prompt),
                name=f"agent-search-{job.index}",
                daemon=True,
            )
        self.status = "running"
        self._publish(ctx)
        for job in jobs:
            job.thread.start()

    def _work(self, job: _AgentJob, prompt: str) -> None:
        sem = self._sem
        if sem is not None:
            while not sem.acquire(timeout=SEMAPHORE_POLL_SEC):
                if self._cancel.is_set():
                    job.status = "error"
                    job.reason = "cancelled"
                    return
        if self._cancel.is_set():
            if sem is not None:
                sem.release()
            job.status = "error"
            job.reason = "cancelled"
            return
        try:
            runner = self._runner_factory(
                AgentSpec(command=job.command, timeout_sec=self._timeout())
            )
            job.output = runner.run(
                prompt, cancel=self._cancel, env=proxy_env(self.settings)
            )
        except AgentError as exc:
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one agent must not crash discovery
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            if sem is not None:
                sem.release()

    def finish(self, ctx: ProviderContext | None = None) -> list[Candidate]:
        if self._candidates is not None:
            self._publish(ctx)
            return self._candidates
        if not self._jobs:
            self._publish(ctx)
            return []
        self._wait_for_jobs(ctx)
        for job in self._jobs:
            self._consume_job(job, ctx)
        candidates = self._merged(ctx)
        self._candidates = candidates
        self.status = self._final_status()
        self._publish(ctx)
        return candidates

    def _wait_for_jobs(self, ctx: ProviderContext | None) -> None:
        """Join every agent against ONE shared wall-clock deadline.

        Agents that already finished keep their output; agents still alive when
        the deadline passes are cancelled, joined for a short grace, and marked
        as a timeout on their 1-based index. A cancellation from the run stops
        the wait right away instead of holding the job until the deadline.
        """
        external = getattr(ctx, "cancel", None) if ctx is not None else None
        for job in self._jobs:
            thread = job.thread
            if thread is None:
                continue
            while thread.is_alive():
                if external is not None and external.is_set():
                    self.cancel()
                    return
                remaining = None
                if self._deadline is not None:
                    remaining = max(0.0, self._deadline - time.monotonic())
                    if remaining <= 0:
                        break
                thread.join(
                    JOIN_SLICE_SEC if remaining is None else min(JOIN_SLICE_SEC, remaining)
                )
        stalled = [
            job
            for job in self._jobs
            if job.thread is not None and job.thread.is_alive()
        ]
        if not stalled:
            return
        self._cancel.set()
        for job in stalled:
            job.thread.join(CANCEL_GRACE_SEC)
        for job in stalled:
            job.status = "error"
            job.reason = "timeout"
            job.output = None
            self._warn(
                ctx,
                f"agent_search: agent {job.index} failed: "
                f"timeout after {self._timeout():g}s",
            )

    def cancel(self, wait: float = CANCEL_GRACE_SEC) -> None:
        """Stop every agent run and its child process when discovery is cancelled."""
        self._cancel.set()
        if self._started and self._candidates is None:
            self.status = "error"
        for job in self._jobs:
            if job.thread is not None and job.thread.is_alive():
                job.thread.join(wait)

    def _consume_job(self, job: _AgentJob, ctx: ProviderContext | None) -> None:
        """Turn one agent's raw output into candidates or an indexed warning."""
        if job.status == "error":
            return
        if job.error is not None:
            self._warn(ctx, f"agent_search: agent {job.index} failed: {job.error}")
            job.status = "error"
            return
        text = job.output or ""
        if not text.strip():
            self._warn(ctx, f"agent_search: agent {job.index} failed: returned no output")
            job.status = "error"
            return
        if len(text.encode("utf-8")) > MAX_STDOUT_BYTES:
            self._warn(
                ctx,
                f"agent_search: agent {job.index} failed: output is larger than 1 MB; ignored",
            )
            job.status = "error"
            return
        plan = parse_payload(text)
        if plan.items:
            job.candidates = [
                Candidate(
                    username=item["username"],
                    source="agent_search",
                    meta={"why": item["why"], "url": item["url"]},
                )
                for item in plan.items
            ]
            job.status = "ok"
            if plan.problems:
                self._warn(
                    ctx,
                    f"agent_search: agent {job.index}: {self._invalid_warning(plan.problems)}",
                )
        elif plan.problems:
            reason = str(plan.problems[0].get("reason") or "invalid entry")
            self._warn(
                ctx,
                f"agent_search: agent {job.index}: invalid output "
                f"(could not parse agent output: {reason})",
            )
            job.status = "error"
        else:
            self._warn(ctx, f"agent_search: agent {job.index}: the agent returned no channels")
            job.status = "empty"

    def _merged(self, ctx: ProviderContext | None) -> list[Candidate]:
        """Dedupe by username in first-seen order, then apply the shared cap."""
        merged: list[Candidate] = []
        seen: set[str] = set()
        for job in self._jobs:
            for candidate in job.candidates:
                key = (candidate.username or "").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(candidate)
        limit = get_int(self.settings, "agent_search_max")
        limit = DEFAULT_MAX if limit is None else max(limit, 0)
        if len(merged) > limit:
            merged = merged[:limit]
            self._warn(
                ctx,
                f"agent_search: kept the first {limit} channels (agent_search_max)",
            )
        return merged

    def _final_status(self) -> str:
        """Classify the batch from what each agent did, not from the capped list.

        An agent that produced valid candidates answered even when the shared
        ``agent_search_max`` cap dropped every one of them, so the batch stays
        ``ok``; ``error`` needs every attempted agent to have failed.
        """
        statuses = [job.status for job in self._jobs]
        if not statuses:
            return "disabled"
        if "ok" in statuses:
            return "ok"
        if all(status == "error" for status in statuses):
            return "error"
        # Any agent that answered (even with nothing) is an answer, so a mixed
        # empty/error batch stays ``empty`` with the failure visible in warnings.
        return "empty"

    def discover(self, ctx: ProviderContext) -> list[Candidate]:
        """Synchronous path: ``find`` calls ``start`` then ``finish`` here.

        The Web UI job path drives ``start``/``finish`` itself so the agents run
        in the background while the other sources work.
        """
        self.start(ctx)
        return self.finish(ctx)


def build_provider(settings: dict[str, str], http: Any) -> AgentSearchProvider:
    return AgentSearchProvider(settings, http)
