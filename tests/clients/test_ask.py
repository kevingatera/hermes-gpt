import asyncio
from types import SimpleNamespace

from hermes_gpt.clients.codex.ask import register_ask_tool


class Registry:
    def add_tool(self, tool, **kwargs):
        self.ask = tool


def setup_controls(state='completed', result_success=True):
    calls = []
    def start(**kwargs):
        calls.append(kwargs)
        return {'success': True, 'job_id': 'a' * 32}
    controls = SimpleNamespace(hermes_session_start=start, hermes_session_continue=start,
        hermes_session_job_status=lambda job_id: {'success': True, 'job': {'status': state, 'session_id': 'session'}},
        hermes_session_job_result=lambda job_id: {'success': result_success, 'output': 'answer'})
    server = Registry()
    register_ask_tool(server, controls, dict)
    return server, calls


def test_ask_returns_completed_answer_and_preserves_profile_defaults():
    server, calls = setup_controls()
    result = asyncio.run(server.ask('request', 'chatgpt', wait_seconds=0))
    assert result['success'] and result['result']['output'] == 'answer'
    assert calls[0]['model'] is None and calls[0]['reasoning_effort'] is None


def test_ask_returns_pending_job_without_duplicate_submission():
    server, calls = setup_controls('running')
    result = asyncio.run(server.ask('request', 'chatgpt', session_id='existing', wait_seconds=0))
    assert result['status'] == 'running'
    assert 'Poll' in result['next_action']
    assert len(calls) == 1 and calls[0]['session_id'] == 'existing'


def test_ask_does_not_label_failed_job_complete():
    server, _ = setup_controls('failed', False)
    result = asyncio.run(server.ask('request', 'chatgpt', wait_seconds=0))
    assert result['success'] is False and result['status'] == 'failed'


def test_invalid_wait_never_starts_work():
    server, calls = setup_controls()
    result = asyncio.run(server.ask('request', 'chatgpt', wait_seconds=31))
    assert result['code'] == 'INVALID_WAIT_SECONDS' and not calls
