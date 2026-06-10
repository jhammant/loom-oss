"""Small shared helpers: terminal styling and errors."""
from __future__ import annotations

import os
import sys

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class LoomError(Exception):
    """A user-facing error. The CLI prints its message and exits non-zero."""


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def cyan(t: str) -> str:
    return _c("36", t)


def info(msg: str) -> None:
    print(f"{cyan('•')} {msg}")


def ok(msg: str) -> None:
    print(f"{green('✓')} {msg}")


def warn(msg: str) -> None:
    print(f"{yellow('!')} {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"{red('✗')} {msg}", file=sys.stderr)
