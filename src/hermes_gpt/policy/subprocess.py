"""Run bounded child processes without invoking a shell."""

from __future__ import annotations

from typing import Any


def run_argv(
    argv: list[str],
    *,
    timeout: int = 120,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    max_output_chars: int = 4096,
    timeout_cap: int = 600,
) -> tuple[int, str, str]:
    """Run ``argv`` with bounded output and process-group cleanup.

    Every invocation gets its own process group on POSIX. If the command
    times out, terminate the entire group before collecting stdout/stderr so
    descendants cannot keep the pipes open and wedge the MCP tool call.
    ``timeout_cap`` defaults to 10 minutes; narrowly scoped callers such as
    the cron runner may explicitly raise it, up to the hard two-hour ceiling.
    """
    import os
    import signal
    import subprocess
    import time

    if not isinstance(argv, list) or not argv:
        raise ValueError("argv must be a non-empty list")

    safe_timeout_cap = max(1, min(int(timeout_cap), 7200))
    capped_timeout = max(1, min(int(timeout), safe_timeout_cap))
    capped_output = max(1, min(int(max_output_chars), 1_048_576))

    popen_kwargs: dict[str, Any] = {
        "cwd": workdir,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "shell": False,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError as exc:
        return (127, "", _truncate(str(exc), capped_output))

    def terminate_process_group() -> None:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                return

            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except (ProcessLookupError, OSError):
                    return
                time.sleep(0.02)

            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        else:
            try:
                proc.kill()
            except OSError:
                pass

    try:
        out, err = proc.communicate(timeout=capped_timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group()
        try:
            out, err = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            out, err = proc.communicate()

        if not out and isinstance(exc.stdout, str):
            out = exc.stdout
        if not err and isinstance(exc.stderr, str):
            err = exc.stderr

        return (
            124,
            _truncate(out or "", capped_output),
            _truncate(err or f"timed out after {capped_timeout}s", capped_output),
        )

    return (
        proc.returncode,
        _truncate(out or "", capped_output),
        _truncate(err or "", capped_output),
    )


def _truncate(text: str, limit: int = 4096) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
