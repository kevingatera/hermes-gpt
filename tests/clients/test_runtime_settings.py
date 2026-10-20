import json
import os

import pytest

from hermes_gpt.clients.chatgpt.management import connection_settings
from hermes_gpt.policy import runtime_settings as settings
from hermes_gpt.policy.authorization import OperatorPolicy


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv(settings.ADMIN_ENV, '1')
    monkeypatch.setenv(settings.PROFILE_CEILING_ENV, 'default,chatgpt')
    monkeypatch.setenv(settings.PROFILE_ENVS[0], 'chatgpt')
    monkeypatch.setenv(settings.PROFILE_ENVS[1], 'chatgpt')
    return tmp_path


def test_preview_confirmation_persistence_and_live_reads(live):
    preview = settings.configure(profiles=['default'], features={'scheduling': True})
    assert preview['success'] and preview['dry_run']
    assert not settings.settings_path().exists()
    assert settings.configure(dry_run=False)['code'] == 'CONFIRMATION_REQUIRED'
    result = settings.configure(profiles=['default'], features={'scheduling': True}, confirm=True, dry_run=False)
    assert result['success'] and result['refresh_required'] is False
    assert settings.getenv('HERMES_GPT_ENABLE_CRON') == '1'
    assert OperatorPolicy().allowed_profiles == ['default']
    assert os.stat(settings.settings_path()).st_mode & 0o777 == 0o600
    assert connection_settings()['profiles'] == ['default']


def test_ceiling_and_revision(live):
    assert not settings.configure(profiles=['unapproved'])['success']
    assert not settings.configure(features={'unknown': True})['success']
    assert not settings.configure(features={'changes': 'yes'})['success']
    assert settings.configure(expected_revision=True)['code'] == 'INVALID_REVISION'
    assert settings.configure(expected_revision=8)['code'] == 'SETTINGS_CONFLICT'


def test_empty_and_corrupt_policy_deny_profiles(live):
    assert settings.configure(profiles=[], confirm=True, dry_run=False)['success']
    assert OperatorPolicy().allowed_profiles == []
    settings.settings_path().write_text('{broken')
    assert OperatorPolicy().allowed_profiles == []
    assert settings.getenv('HERMES_GPT_ALLOW_WRITE') == '0'
    assert connection_settings()['success'] is False


def test_administration_cannot_be_self_granted(live, monkeypatch):
    settings.settings_path().write_text(json.dumps({'features': {'changes': True}}))
    monkeypatch.delenv(settings.ADMIN_ENV)
    assert settings.configure()['code'] == 'CONNECTION_ADMIN_DISABLED'
    assert settings.getenv('HERMES_GPT_ALLOW_WRITE') != '1'


def test_catalogue_and_configuration_on_same_connection(live, monkeypatch):
    import asyncio

    from hermes_gpt.clients.chatgpt.management import register_management
    from hermes_gpt.mcp_compat import HermesMCP
    from tests.conftest import wire
    server = HermesMCP('live-settings-test', version='test')
    register_management(server, callbacks={})
    async def exercise():
        names = [tool.name for tool in await server.list_tools()]
        result = wire(await server.call_tool('hermes_connection_configure', {
            'features': {'delegation': False}, 'confirm': True, 'dry_run': False}))
        assert result['structuredContent']['success']
        current = wire(await server.call_tool('hermes_connection_settings', {}))
        assert not current['structuredContent']['features']['delegation']
        assert names == [tool.name for tool in await server.list_tools()]
    asyncio.run(exercise())


def test_children_receive_live_policy_without_connection_administration(live):
    from hermes_gpt.sessions.session import _profile_runtime
    (live / 'profiles/chatgpt').mkdir(parents=True)
    assert settings.configure(features={'web': True}, profiles=['default', 'chatgpt'], confirm=True, dry_run=False)['success']
    _, child = _profile_runtime('chatgpt', live)
    assert child['HERMES_GPT_ENABLE_WEB'] == '1'
    assert child[settings.ADMIN_ENV] == '0'
    assert child[settings.PROFILE_ENVS[1]] == 'default,chatgpt'


def test_disabled_session_tools_can_be_enabled_without_rediscovery(live, monkeypatch):
    import asyncio

    from hermes_gpt.server import app
    from tests.conftest import wire

    monkeypatch.setenv('HERMES_GPT_CODEX_TOOLSET', 'sessions')
    monkeypatch.setenv('HERMES_GPT_ENABLE_SESSION_CONTROL', '0')
    monkeypatch.setenv('HERMES_GPT_ENABLE_SESSION_SEARCH', '0')
    monkeypatch.setenv('HERMES_GPT_ENABLE_SCOPED_TASKS', '0')
    server = app.build_codex_mcp_server()

    async def exercise():
        catalogue = [tool.name for tool in await server.list_tools()]
        assert {'hermes_session_start', 'hermes_session_list', 'hermes_task_start'} <= set(catalogue)
        disabled = wire(await server.call_tool('hermes_session_profiles', {}))['structuredContent']
        assert disabled['code'] == 'SESSION_CONTROL_DISABLED'
        changed = wire(await server.call_tool('hermes_connection_configure', {
            'features': {'delegation': True}, 'profiles': ['default'],
            'confirm': True, 'dry_run': False}))['structuredContent']
        assert changed['success']
        enabled = wire(await server.call_tool('hermes_session_profiles', {}))['structuredContent']
        assert enabled['success'] and enabled['profiles'][0]['profile'] == 'default'
        assert catalogue == [tool.name for tool in await server.list_tools()]
    asyncio.run(exercise())
