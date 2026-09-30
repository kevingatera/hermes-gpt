import asyncio
import os

import pytest

from hermes_gpt.server import request_log
from tests.conftest import wire


def test_trace_links_success_and_failure_without_payloads(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    server = request_log.TracedMCP('trace-test', version='test')
    def hermes_test(prompt: str):
        return {'success': False, 'error': {'code': 'PROFILE_NOT_ALLOWED', 'message': prompt},
                'job_id': 'a' * 32}
    server.add_tool(hermes_test)
    result = asyncio.run(server.call_tool('hermes_test', {'prompt': 'private-email-and-key'}))
    records = request_log.request_diagnostics()['records']
    assert [r['phase'] for r in records] == ['started', 'finished']
    assert records[-1]['error_code'] == 'PROFILE_NOT_ALLOWED'
    assert records[-1]['outcome'] == 'error'
    assert records[-1]['job_id'] == 'a' * 32
    assert records[0]['request_id'] == records[-1]['request_id']
    if request_log.SDK_V2:
        assert wire(result)['_meta']['hermes_request_id'] == records[0]['request_id']
    assert 'private-email-and-key' not in request_log.log_path().read_text()
    assert len(request_log.request_diagnostics(trace_id=records[0]['request_id'])['records']) == 2
    assert request_log.request_id.get() is None
    if os.name != 'nt':
        assert request_log.log_path().stat().st_mode & 0o777 == 0o600


def test_exception_logs_type_only_and_preserves_failure(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    server = request_log.TracedMCP('trace-test', version='test')
    def hermes_crash():
        raise ValueError('private exception payload')
    server.add_tool(hermes_crash)
    if request_log.SDK_V2:
        from mcp.server.mcpserver.exceptions import UnexpectedToolError as ToolFailure
    else:
        from mcp.server.fastmcp.exceptions import ToolError as ToolFailure
    with pytest.raises(ToolFailure):
        asyncio.run(server.call_tool('hermes_crash', {}))
    records = request_log.request_diagnostics()['records']
    assert records[-1]['outcome'] == 'error'
    assert 'exception_type' in records[-1]
    assert 'private exception payload' not in request_log.log_path().read_text()
    assert request_log.request_id.get() is None


def test_diagnostics_validation_and_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    for n in range(120):
        request_log._write({'request_id': f'{n:032x}', 'phase': 'finished'})
    assert len(request_log.request_diagnostics(limit=100)['records']) == 100
    assert request_log.request_diagnostics(limit=True)['success'] is False
    assert request_log.request_diagnostics(trace_id='../private')['success'] is False
    assert request_log.request_diagnostics(limit=101)['success'] is False
    assert request_log._summary({'bad': 'shape'})['outcome'] == 'ok'
