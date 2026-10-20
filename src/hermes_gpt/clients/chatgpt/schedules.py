"""Schedule changes through Hermes' canonical CLI and scheduler locking."""

import re
from pathlib import Path
from typing import Any

from hermes_gpt.policy import authorization as op
from hermes_gpt.policy import runtime_settings
from hermes_gpt.sessions.job_store import _data_root
from hermes_gpt.sessions.jobs import MODEL_ID_RE, REASONING_EFFORTS, _hermes_executable
from hermes_gpt.workspace.cron_store import _find_job, _read_jobs


def _change(profile: str, arguments: list[str], *, confirm: bool, dry_run: bool,
            job_id: str | None = None, prompt: str = '', root: Path | None = None,
            runner=None, agent_root: Path | None = None) -> dict[str, Any]:
    if runtime_settings.getenv('HERMES_GPT_ALLOW_CRON_WRITE') != '1':
        return {'success': False, 'code': 'SCHEDULE_CHANGES_DISABLED',
                'suggested_action': 'On user request, enable schedule_changes using hermes_connection_configure.'}
    if not isinstance(confirm, bool) or not isinstance(dry_run, bool):
        return {'success': False, 'code': 'INVALID_CONFIRMATION'}
    if not dry_run and not confirm:
        return {'success': False, 'code': 'CONFIRMATION_REQUIRED'}
    if not dry_run and runtime_settings.getenv('HERMES_GPT_ALLOW_WRITE') != '1':
        return {'success': False, 'code': 'CHANGES_DISABLED',
                'suggested_action': 'On user request, enable changes using hermes_connection_configure.'}
    try:
        root = _data_root(root)
        policy = op.OperatorPolicy()
        policy.require_level('cron')
        policy.require_profile(profile, root)
        home = op.resolve_profile_home(profile, root)
        before = _read_jobs(home)
        if job_id is not None:
            # Resolve an exact existing ID. User text never becomes an option.
            job = _find_job(before, job_id)
            if not job:
                return {'success': False, 'code': 'SCHEDULE_NOT_FOUND'}
            job_id = str(job['id'])
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', job_id):
                return {'success': False, 'code': 'INVALID_SCHEDULE_ID'}
            arguments = [arguments[0], job_id, *arguments[1:]]
        argv = [_hermes_executable(agent_root), '--profile', profile, 'cron', *arguments]
        preview = policy.effective_dry_run(dry_run)
        if preview:
            return {'success': True, 'dry_run': True, 'action': arguments[0],
                    'profile': profile, 'job_id': job_id, 'changed': False}
        policy.require_mutation(dry_run)
        rc, output, _ = (runner or op.run_argv)(argv, timeout=30, workdir=None)
        after = _read_jobs(home)
        # CLI output can contain private prompts. Return IDs and observed state.
        if arguments[0] == 'create' and rc == 0:
            match = re.search(r'Created job: ([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})', output)
            job_id = match.group(1) if match else None
            if job_id is None or not _find_job(after, job_id):
                return {'success': False, 'code': 'SCHEDULE_CREATION_UNVERIFIED',
                        'changed': True, 'suggested_action': 'Inspect cron_list before retrying; the command succeeded but its created job was not verified.'}
        op.audit_record(tool='hermes_schedule_' + arguments[0], level=policy.level,
                        apply_mode=policy.apply_mode, dry_run=False, success=rc == 0,
                        changed=(rc == 0),
                        profile=profile, job_id=job_id,
                        summary='canonical scheduler command', prompt=prompt)
        return {'success': rc == 0, 'dry_run': False, 'changed': rc == 0,
                'job_id': job_id, 'exit_code': rc,
                'code': None if rc == 0 else 'SCHEDULE_COMMAND_FAILED',
                'action': arguments[0]}
    except (OSError, ValueError, PermissionError):
        return {'success': False, 'code': 'SCHEDULE_AUTHORITY_OR_INPUT_INVALID'}


def create(profile: str, schedule: str, prompt: str, name: str,
           paused: bool = True, model: str | None = None,
           reasoning_effort: str | None = None, confirm: bool = False,
           dry_run: bool = True, root: Path | None = None, runner=None, agent_root: Path | None = None) -> dict[str, Any]:
    for value, limit in ((schedule, 200), (prompt, 20000), (name, 200)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
            return {'success': False, 'code': 'INVALID_SCHEDULE_INPUT'}
    if not isinstance(paused, bool) or (model is not None and not MODEL_ID_RE.fullmatch(model)):
        return {'success': False, 'code': 'INVALID_SCHEDULE_INPUT'}
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        return {'success': False, 'code': 'INVALID_SCHEDULE_INPUT'}
    args = ['create', '--name=' + name, '--deliver', 'local', '--failure-deliver', 'local']
    if paused:
        args += ['--paused', '--paused-reason', 'Created from ChatGPT; resume when ready']
    if model:
        args += ['--model', model]
    if reasoning_effort:
        args += ['--reasoning-effort', reasoning_effort]
    # End options before schedule/prompt; values beginning with '-' stay data.
    args += ['--', schedule, prompt]
    return _change(profile, args, confirm=confirm, dry_run=dry_run, prompt=prompt, root=root, runner=runner, agent_root=agent_root)


def action(profile: str, job_id: str, operation: str, *, confirm: bool = False,
           dry_run: bool = True, root: Path | None = None, runner=None, agent_root: Path | None = None) -> dict[str, Any]:
    if operation not in ('pause', 'resume', 'run', 'remove'):
        return {'success': False, 'code': 'INVALID_SCHEDULE_ACTION'}
    return _change(profile, [operation], job_id=job_id, confirm=confirm,
                   dry_run=dry_run, root=root, runner=runner, agent_root=agent_root)
