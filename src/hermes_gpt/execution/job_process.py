"""Collect PID-reuse-resistant identities from operating-system process data."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def _cmdline_hash(raw: bytes | str) -> str:
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


def _procfs_identity(pid: int) -> dict[str, str] | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    cmd_path = Path("/proc") / str(pid) / "cmdline"
    try:
        stat = stat_path.read_text(encoding="utf-8", errors="replace")
        right = stat.rfind(")")
        if right < 0:
            return None
        tail = stat[right + 2 :].split()
        if len(tail) <= 19:
            return None
        cmdline = cmd_path.read_bytes()
    except OSError:
        return None
    return {
        "platform": "procfs",
        "start_token": tail[19],
        "cmdline_sha256": _cmdline_hash(cmdline),
    }


def _ps_identity(pid: int) -> dict[str, str] | None:
    try:
        started = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if started.returncode != 0 or command.returncode != 0:
        return None
    start_token = started.stdout.strip()
    cmdline = command.stdout.strip()
    if not start_token or not cmdline:
        return None
    return {
        "platform": "ps",
        "start_token": start_token,
        "cmdline_sha256": _cmdline_hash(cmdline),
    }


def _windows_identity(pid: int) -> dict[str, str] | None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        return None
    script = (
        f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={pid}";'
        "if($null -eq $p){exit 3};"
        "$p|Select-Object ProcessId,CreationDate,CommandLine|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        data = json.loads(completed.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    creation = str(data.get("CreationDate") or "").strip()
    command = str(data.get("CommandLine") or "").strip()
    if not creation or not command:
        return None
    return {
        "platform": "windows-cim",
        "start_token": creation,
        "cmdline_sha256": _cmdline_hash(command),
    }
