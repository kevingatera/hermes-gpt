import json

import pytest

from hermes_gpt.clients.chatgpt import schedules


@pytest.fixture
def scheduler(monkeypatch, tmp_path):
    monkeypatch.setattr(schedules, "_hermes_executable", lambda root: "hermes")
    monkeypatch.setenv('HERMES_GPT_OPERATOR_ENABLED', '1')
    monkeypatch.setenv('HERMES_GPT_OPERATOR_LEVEL', 'cron')
    monkeypatch.setenv('HERMES_GPT_OPERATOR_APPLY_MODE', 'direct')
    monkeypatch.setenv('HERMES_GPT_OPERATOR_ALLOWED_PROFILES', 'default')
    monkeypatch.setenv('HERMES_GPT_ALLOW_CRON_WRITE', '1')
    monkeypatch.setenv('HERMES_GPT_ALLOW_WRITE', '1')
    (tmp_path / 'config.yaml').write_text('{}')
    directory = tmp_path / 'cron'
    directory.mkdir()
    (directory / 'jobs.json').write_text(json.dumps({'jobs': [{'id': 'safe-id', 'name': 'test'}]}))
    return tmp_path


def test_confirmation_and_preview_never_run(scheduler):
    def forbidden(*args, **kwargs):
        pytest.fail('Unexpected subprocess')
    assert schedules.action('default', 'safe-id', 'remove', root=scheduler, runner=forbidden)['dry_run']
    assert schedules.action('default', 'safe-id', 'remove', dry_run=False, root=scheduler, runner=forbidden)['code'] == 'CONFIRMATION_REQUIRED'
    assert schedules.action('default', '--option', 'run', confirm=True, dry_run=False, root=scheduler, runner=forbidden)['code'] == 'SCHEDULE_NOT_FOUND'


def test_native_command_and_private_output(scheduler):
    captured = []
    def runner(argv, **kwargs):
        captured.append(argv)
        return 0, 'private prompt and credentials', ''
    result = schedules.action('default', 'safe-id', 'run', confirm=True, dry_run=False, root=scheduler, runner=runner)
    assert result['success']
    assert captured == [['hermes', '--profile', 'default', 'cron', 'run', 'safe-id']]
    assert 'private' not in json.dumps(result)


def test_create_uses_local_delivery_paused_and_ends_options(scheduler):
    captured = []
    def runner(argv, **kwargs):
        captured.append(argv)
        (scheduler / 'cron/jobs.json').write_text(json.dumps({'jobs': [{'id': 'new-id', 'name': 'test'}]}))
        return 0, 'Created job: new-id', ''
    result = schedules.create('default', 'every 2h', '--not-an-option', 'test', confirm=True, dry_run=False, root=scheduler, runner=runner)
    assert result['success']
    assert '--paused' in captured[0]
    assert captured[0][-3:] == ['--', 'every 2h', '--not-an-option']
    assert captured[0][captured[0].index('--deliver')+1] == 'local'


def test_live_write_disable_blocks_actions(scheduler, monkeypatch):
    monkeypatch.setenv('HERMES_GPT_ALLOW_WRITE', '0')
    result = schedules.action('default', 'safe-id', 'run', confirm=True,
                              dry_run=False, root=scheduler)
    assert result['code'] == 'CHANGES_DISABLED'
