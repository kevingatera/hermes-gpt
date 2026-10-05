import asyncio
from types import SimpleNamespace

from hermes_gpt.clients.codex.ask import register_ask_tool


class Registry:
    def add_tool(self, tool, **kwargs):
        setattr(self, "ask" if tool.__name__ == "hermes_ask" else "wait", tool)


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
    assert 'hermes_wait' in result['next_action']
    assert len(calls) == 1 and calls[0]['session_id'] == 'existing'


def test_ask_does_not_label_failed_job_complete():
    server, _ = setup_controls('failed', False)
    result = asyncio.run(server.ask('request', 'chatgpt', wait_seconds=0))
    assert result['success'] is False and result['status'] == 'failed'


def test_invalid_wait_never_starts_work():
    server, calls = setup_controls()
    result = asyncio.run(server.ask('request', 'chatgpt', wait_seconds=31))
    assert result['code'] == 'INVALID_WAIT_SECONDS' and not calls


def test_wait_observes_existing_job_without_submission():
    server, calls = setup_controls()
    result = asyncio.run(server.wait('a' * 32, wait_seconds=0))
    assert result['status'] == 'completed' and result['result']['output'] == 'answer'
    assert not calls


def test_wait_pending_and_invalid_inputs_never_submit():
    server, calls = setup_controls('running')
    assert asyncio.run(server.wait('a' * 32, wait_seconds=0))['status'] == 'running'
    for value in (True, -1, 31, '25'):
        assert asyncio.run(server.wait('a' * 32, wait_seconds=value))['code'] == 'INVALID_WAIT_SECONDS'
    assert not calls


def test_wait_returns_answer_after_running_observation(monkeypatch):
    from hermes_gpt.clients.codex import job_wait
    _server, calls = setup_controls()
    observed = iter([
        {'success': True, 'job': {'status': 'running'}},
        {'success': True, 'job': {'status': 'completed', 'session_id': 'session'}},
    ])
    # Both registered tools share the same controls object captured by setup.
    async def sleep(_seconds):
        pass
    monkeypatch.setattr(job_wait.asyncio, 'sleep', sleep)
    controls = SimpleNamespace(
        hermes_session_job_status=lambda _: next(observed),
        hermes_session_job_result=lambda _: {'success': True, 'response': 'finished'},
    )
    result = asyncio.run(job_wait.wait_for_job(controls, 'a' * 32, 25))
    assert result['result']['response'] == 'finished'
    assert not calls


def test_cancelled_observation_does_not_cancel_job():
    from hermes_gpt.clients.codex.job_wait import wait_for_job
    calls = []
    controls = SimpleNamespace(
        hermes_session_job_status=lambda _: {'success': True, 'job': {'status': 'running'}},
        hermes_session_job_cancel=lambda _: calls.append('cancel'),
    )
    async def observe():
        task = asyncio.create_task(wait_for_job(controls, 'a' * 32, 25))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(observe())
    assert calls == []


def test_existing_status_tool_returns_metadata_and_answer_without_submission():
    from hermes_gpt.clients.codex.job_wait import register_status_wait_tool
    controls = SimpleNamespace(
        hermes_session_job_status=lambda _: {'success': True, 'job': {'status': 'completed', 'profile': 'default'}},
        hermes_session_job_result=lambda _: {'success': True, 'response': 'observed answer'},
    )
    server = Registry()
    register_status_wait_tool(server, controls, dict)
    result = asyncio.run(server.wait('a' * 32, wait_seconds=0))
    assert result['job']['profile'] == 'default'
    assert result['result']['response'] == 'observed answer'
