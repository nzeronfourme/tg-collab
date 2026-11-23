"""Output helpers: JSON on stdout only, human text through Rich."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console

stderr_console = Console(stderr=True)


def emit_json(payload: Any) -> None:
    """Write exactly one UTF-8 JSON document (one line) to stdout."""
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(line.encode("utf-8"))
        buffer.flush()
    else:
        sys.stdout.write(line)
        sys.stdout.flush()


def write_stderr(message: str) -> None:
    """Write one UTF-8 diagnostic line to stderr, independent of the console codepage."""
    line = message.rstrip("\n") + "\n"
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is not None:
        buffer.write(line.encode("utf-8"))
        buffer.flush()
    else:
        sys.stderr.write(line)
        sys.stderr.flush()


def human_int(value: int | None) -> str:
    return "-" if value is None else f"{value:,}".replace(",", " ")


def human_float(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"
