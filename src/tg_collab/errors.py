"""CLI error types and the process exit-code policy."""

from __future__ import annotations


class TgCollabError(Exception):
    """A user-facing or data error: message to stderr, exit code 1 by default."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class UsageError(TgCollabError):
    """Bad invocation: exit code 2."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class ChannelNotFound(TgCollabError):
    """A username has no public t.me identity, so it is not a real channel."""
