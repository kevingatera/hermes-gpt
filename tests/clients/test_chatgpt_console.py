import asyncio
from types import SimpleNamespace

from hermes_gpt.clients.chatgpt import console
from hermes_gpt.mcp_compat import HermesMCP
from tests.conftest import wire


def build(monkeypatch, enabled=True, controls_enabled=True):
    monkeypatch.setenv(console.UI_ENV, '1' if enabled else '0')
    server = HermesMCP('ui-test', version='test')
    core = SimpleNamespace(capabilities=lambda: {'capabilities': {'delegation': {'enabled': controls_enabled}}})
    controls = SimpleNamespace(hermes_session_profiles=lambda: {'success': True, 'profiles': [{'profile': 'authorized'}]})
    console.register_console(server, core=core, controls=controls, controls_enabled=controls_enabled)
    return server


def test_console_is_opt_in(monkeypatch):
    server = build(monkeypatch, enabled=False)
    assert not asyncio.run(server.list_tools())
    assert not asyncio.run(server.list_resources())


def test_console_is_read_only_and_references_a_self_contained_resource(monkeypatch):
    server = build(monkeypatch)
    tool = wire(asyncio.run(server.list_tools())[0])
    assert tool['_meta']['ui']['resourceUri'] == console.UI_URI
    assert tool['annotations']['readOnlyHint'] is True
    result = wire(asyncio.run(server.call_tool('hermes_console', {})))
    assert result['structuredContent']['profiles']['profiles'][0]['profile'] == 'authorized'
    resources = asyncio.run(server.list_resources())
    resource = wire(resources[0])
    assert resource['mimeType'] == console.UI_MIME
    assert resource['_meta']['ui']['csp'] == {'connectDomains': [], 'resourceDomains': []}
    contents = list(asyncio.run(server.read_resource(console.UI_URI)))
    html = contents[0].content
    assert '/* PANEL_SCRIPT */' not in html
    assert 'ui/initialize' in html and 'tools/call' in html
    assert 'fetch(' not in html and 'localStorage' not in html
    assert 'innerHTML' not in html


def test_console_does_not_enable_session_controls(monkeypatch):
    server = build(monkeypatch, controls_enabled=False)
    result = wire(asyncio.run(server.call_tool('hermes_console', {})))
    assert result['structuredContent']['profiles']['profiles'] == []
    assert result['structuredContent']['capabilities']['capabilities']['delegation']['enabled'] is False


def test_console_can_follow_a_job_without_resubmitting(monkeypatch):
    server = build(monkeypatch)
    result = wire(asyncio.run(server.call_tool('hermes_console', {'job_id': 'a' * 32})))
    assert result['structuredContent']['follow_job_id'] == 'a' * 32
    invalid = wire(asyncio.run(server.call_tool('hermes_console', {'job_id': 'invalid'})))
    assert invalid['structuredContent']['code'] == 'INVALID_JOB_ID'
