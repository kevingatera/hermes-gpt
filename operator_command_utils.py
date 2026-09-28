"""Command parsing shared by the bounded runner and Owner tools."""

from __future__ import annotations

import os
import shlex


def _split_command_argv(command: str) -> list[str]:
    """Split a command string into argv without invoking a shell.

    On Windows, shlex with posix=False preserves surrounding quote characters.
    That makes a quoted single argument like "45 5 * * *" arrive downstream as
    a literal string that still includes the quotes. Strip only matching outer
    quotes after parsing so cron expressions and paths with spaces survive as
    single argv tokens, without changing unquoted Windows paths.
    """
    argv = shlex.split(command, posix=(os.name != "nt"))
    if os.name == "nt":
        cleaned: list[str] = []
        for arg in argv:
            if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in {"'", '"'}:
                cleaned.append(arg[1:-1])
            else:
                cleaned.append(arg)
        return cleaned
    return argv
