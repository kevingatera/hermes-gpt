"""Read, sanitize, and atomically write Hermes cron job records."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

# Preserve the original jobs.json container shape per profile_home so writes
# round-trip list-shaped files and Hermes' canonical {"jobs": [...]} form.
_jobs_shape_cache: dict[str, str] = {}

# Fields that are PRESERVED when copying a job across profiles.
_PRESERVED_COPY_FIELDS: tuple[str, ...] = (
    "name",
    "prompt",
    "schedule",
    "schedule_display",
    "deliver",
    "skills",
    "skill",
    "model",
    "provider",
    "base_url",
    "script",
    "context_from",
    "enabled_toolsets",
    "workdir",
    "no_agent",
    "repeat",  # only repeat.times is preserved; completed is reset below
)

# Fields that are RESET (cleared or zeroed) when copying.
_RESET_FIELDS: tuple[str, ...] = (
    "id",
    "last_run_at",
    "last_status",
    "last_error",
    "last_delivery_error",
    "paused_at",
    "paused_reason",
    "fire_claim",
    "next_run_at",  # will be recomputed by Hermes on next tick / save
    "output",  # any cached output path
)

# Fields that get a specific reset value rather than being cleared.
_RESET_VALUES: dict[str, Any] = {
    "state": "scheduled",
    "enabled": True,
}


def _cron_dir(profile_home: Path) -> Path:
    return profile_home / "cron"


def _jobs_file(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "jobs.json"


def _jobs_shape_key(profile_home: Path) -> str:
    try:
        return str(profile_home.resolve())
    except Exception:  # noqa: BLE001 - cache keys use best-effort path resolution.
        return str(profile_home)


def _read_jobs(profile_home: Path) -> list[dict[str, Any]]:
    """Read jobs.json. Returns [] if missing or unparseable."""
    path = _jobs_file(profile_home)
    if not path.exists():
        _jobs_shape_cache.setdefault(_jobs_shape_key(profile_home), "dict")
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    shape = "dict" if isinstance(data, dict) else "list" if isinstance(data, list) else "dict"
    _jobs_shape_cache[_jobs_shape_key(profile_home)] = shape
    if isinstance(data, dict):
        # Some Hermes versions store {"jobs": [...]}.
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []
    return [j for j in jobs if isinstance(j, dict)]


def _write_jobs(profile_home: Path, jobs: list[dict[str, Any]]) -> None:
    """Atomically write jobs.json. Creates cron dir if missing."""
    cron_dir = _cron_dir(profile_home)
    cron_dir.mkdir(parents=True, exist_ok=True)
    target = _jobs_file(profile_home)
    tmp = target.with_suffix(".json.tmp")
    shape = _jobs_shape_cache.get(_jobs_shape_key(profile_home), "dict")
    payload: Any
    if shape == "list":
        payload = jobs
    else:
        payload = {"jobs": jobs}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, target)


def _hash_prompt(prompt: str | None) -> tuple[int, str]:
    if not prompt:
        return (0, "")
    data = prompt.encode("utf-8", errors="replace")
    return (len(data), hashlib.sha256(data).hexdigest())


def _format_job_safe(job: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe job view with no raw prompt."""
    prompt = str(job.get("prompt") or "")
    prompt_len, prompt_sha = _hash_prompt(prompt)
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    if isinstance(skills, str):
        skills = [skills]
    skills = [str(s) for s in skills if s]
    return {
        "job_id": str(job.get("id") or "unknown"),
        "name": str(job.get("name") or prompt[:50] or (skills[0] if skills else "") or "cron job"),
        "schedule": str(job.get("schedule_display") or job.get("schedule") or "?"),
        "enabled": bool(job.get("enabled", True)),
        "state": str(job.get("state") or ("scheduled" if job.get("enabled", True) else "paused")),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "last_delivery_error": job.get("last_delivery_error"),
        "deliver": job.get("deliver", "local"),
        "skills": skills,
        "workdir": job.get("workdir"),
        "prompt_len": prompt_len,
        "prompt_sha256": prompt_sha,
    }


def _find_job(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    """Find a job by id or name (case-insensitive name match)."""
    if not job_id:
        return None
    needle = str(job_id).strip().lower()
    for job in jobs:
        if str(job.get("id") or "") == job_id:
            return job
        if str(job.get("id") or "").lower() == needle:
            return job
        if str(job.get("name") or "").lower() == needle:
            return job
    return None


def _is_duplicate(
    target_jobs: list[dict[str, Any]], source_job: dict[str, Any]
) -> bool:
    """Return True if target has an active job with same name + schedule."""
    src_name = str(source_job.get("name") or "").lower()
    src_sched = str(source_job.get("schedule_display") or source_job.get("schedule") or "").lower()
    for job in target_jobs:
        if not job.get("enabled", True):
            continue
        tgt_name = str(job.get("name") or "").lower()
        tgt_sched = str(job.get("schedule_display") or job.get("schedule") or "").lower()
        if tgt_name == src_name and tgt_sched == src_sched:
            return True
    return False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
