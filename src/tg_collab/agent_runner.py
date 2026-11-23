"""Local AI agent runner: subprocess only, no LLM client, no shell."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_ARGV_LENGTH = 8000
MAX_STDERR_TAIL = 2000

#: Known local agent CLIs, tried in order only when ``agent_command`` is blank.
AGENT_PRESETS: tuple[str, ...] = (
    "opencode run {prompt}",
    "claude -p {prompt}",
    "codex exec {prompt}",
    "gemini -p {prompt}",
)


class AgentError(RuntimeError):
    """The configured agent command failed or is misconfigured."""


def _split_command(command: str, platform: str | None = None) -> list[str]:
    """Split the configured command for the target platform.

    The ``platform`` argument is injectable so both branches are testable on Windows.
    """
    platform = platform or os.name
    command = (command or "").strip()
    if not command:
        raise AgentError("agent_command is empty")
    if platform == "posix":
        argv = shlex.split(command, posix=True)
    else:
        argv = shlex.split(command, posix=False)
        cleaned: list[str] = []
        for token in argv:
            if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
                token = token[1:-1]
            cleaned.append(token)
        argv = cleaned
    if not argv:
        raise AgentError("agent_command is empty")
    return argv


def _resolve_executable(argv: list[str], platform: str | None = None) -> list[str]:
    """Make a Windows shim launchable without a general shell.

    ``.cmd``/``.bat`` run through ``%COMSPEC% /d /s /c`` and ``.ps1`` through
    PowerShell, with only the resolved shim and its own arguments inside that
    interpreter. ``.exe`` and POSIX commands keep their argv unchanged.
    """
    platform = platform or os.name
    if platform != "nt" or not argv:
        return argv
    program, args = argv[0], argv[1:]
    resolved = program
    if not (os.sep in program or (os.altsep and os.altsep in program)):
        resolved = shutil.which(program) or program
    suffix = Path(resolved).suffix.lower()
    if suffix in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        # cmd.exe treats a newline inside /c as a command separator, so a raw
        # multi-line prompt would run only its first line. Flatten it; the
        # argument content survives, and {prompt_file} is the escape hatch when
        # the agent supports reading a file.
        flat_args = [arg.replace("\r", " ").replace("\n", " ") for arg in args]
        return [comspec, "/d", "/s", "/c", subprocess.list2cmdline([resolved, *flat_args])]
    if suffix == ".ps1":
        powershell = shutil.which("powershell") or "powershell.exe"
        return [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            resolved,
            *args,
        ]
    return [resolved, *args]


def resolve_agent_command(command: str | None = None) -> str:
    """Return the explicit command, or the first preset CLI found on PATH."""
    explicit = (command or "").strip()
    if explicit:
        return explicit
    for preset in AGENT_PRESETS:
        if shutil.which(preset.split()[0]):
            return preset
    raise AgentError(
        "no local agent found (tried opencode, claude, codex, gemini); "
        "set agent_command in Settings"
    )


def _popen_target(argv: list[str], platform: str | None = None) -> list[str] | str:
    """Hand cmd.exe one raw command line; the list form re-escapes its quotes.

    Python's list-to-command-line quoting escapes inner quotes for a C runtime,
    but ``cmd.exe`` parses them itself, so ``gemini -p "two words"`` arrived
    split. A single string with the shim line appended unchanged is exact.
    """
    if (
        (platform or os.name) == "nt"
        and len(argv) == 5
        and Path(argv[0]).name.lower() == "cmd.exe"
        and argv[1:4] == ["/d", "/s", "/c"]
    ):
        return f'"{argv[0]}" /d /s /c {argv[4]}'
    return argv


def _clean_output(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = ANSI_RE.sub("", text)
    return text.strip()


def _remove_tree(path: str) -> None:
    """Remove a scratch tree; retry briefly because a killed child may still hold it."""
    for _ in range(3):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.05)


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


@dataclass(frozen=True)
class AgentSpec:
    command: str
    timeout_sec: float = 300.0


class AgentRunner:
    def __init__(self, spec: AgentSpec, workdir: str | Path | None = None) -> None:
        self.spec = spec
        self.workdir = str(workdir) if workdir is not None else None

    def _communicate(self, process: subprocess.Popen, cancel: Any) -> tuple[bytes, bytes]:
        """Wait for the child while polling ``cancel`` so a stuck run can be stopped."""
        timeout = self.spec.timeout_sec
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                _kill_tree(process)
                raise AgentError("agent cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_tree(process)
                raise AgentError(f"agent timed out after {timeout:g}s")
            try:
                return process.communicate(timeout=min(remaining, 0.1))
            except subprocess.TimeoutExpired:
                continue

    def run(
        self,
        prompt: str,
        workdir: str | Path | None = None,
        cancel: Any = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run the child in an isolated cwd so it cannot touch the caller's directory.

        A caller-provided ``workdir`` (argument or constructor) is used as-is and
        kept; otherwise a throwaway temp directory is created and removed whatever
        way the run ends, including the timeout and cancel kill paths.
        """
        argv = _split_command(self.spec.command)
        has_prompt = any("{prompt}" in token for token in argv)
        has_prompt_file = any("{prompt_file}" in token for token in argv)
        if not has_prompt and not has_prompt_file:
            raise AgentError(
                "agent_command must contain {prompt} or {prompt_file} so the agent receives the task"
            )

        prompt_path: str | None = None
        temp_dir: str | None = None
        try:
            if has_prompt_file:
                handle = tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".md", delete=False
                )
                with handle:
                    handle.write(prompt)
                prompt_path = handle.name
                argv = [
                    token.replace("{prompt_file}", prompt_path).replace("{prompt}", prompt)
                    for token in argv
                ]
            else:
                argv = [token.replace("{prompt}", prompt) for token in argv]
                joined = " ".join(argv)
                if len(joined) > MAX_ARGV_LENGTH:
                    raise AgentError(
                        "the prompt is too long for one command line; switch agent_command to "
                        "use {prompt_file} instead of {prompt}"
                    )

            argv = _resolve_executable(argv)

            child_env = os.environ.copy()
            # Local agents are commonly Python CLIs; make their stdout UTF-8 even on
            # Windows console codepages so Russian drafts do not turn into mojibake.
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env.setdefault("PYTHONUTF8", "1")
            if env:
                child_env.update(env)
            if workdir is not None:
                child_cwd = str(workdir)
            elif self.workdir is not None:
                child_cwd = self.workdir
            else:
                temp_dir = tempfile.mkdtemp(prefix="tg-collab-agent-")
                child_cwd = temp_dir
            try:
                process = subprocess.Popen(
                    _popen_target(argv),
                    cwd=child_cwd,
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise AgentError(f"could not start the agent: {exc}") from None
            stdout, stderr = self._communicate(process, cancel)
        finally:
            if prompt_path:
                try:
                    Path(prompt_path).unlink()
                except OSError:
                    pass
            if temp_dir:
                _remove_tree(temp_dir)

        text = _clean_output(stdout)
        if process.returncode != 0:
            tail = _clean_output(stderr)[-MAX_STDERR_TAIL:]
            raise AgentError(
                f"agent exited with code {process.returncode}"
                + (f": {tail}" if tail else "")
            )
        if not text:
            tail = _clean_output(stderr)[-MAX_STDERR_TAIL:]
            raise AgentError("agent returned empty output" + (f": {tail}" if tail else ""))
        return text


def default_agent_spec(settings: dict[str, str]) -> AgentSpec:
    timeout_raw = (settings.get("agent_timeout_sec") or "300").strip()
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 300.0
    return AgentSpec(resolve_agent_command(settings.get("agent_command")), timeout)
