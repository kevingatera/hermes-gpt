import io
import json
import os
import time
from pathlib import Path

import pytest

import operator_live_events as live_events
import operator_session as session
import operator_session_job_runtime as job_runtime_process
import operator_session_job_store as job_store
import operator_session_jobs as job_runtime


class _ImmediateThread:
    def __init__(self, *, target, args=(), daemon, name=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.name = name

    def start(self):
        self.target(*self.args)


class _FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode = None
        self.stdin = io.StringIO()

    def wait(self, timeout=None):
        self.kwargs["stdout"].write("mock Hermes response token=secret-value-123456789")
        self.kwargs["stdout"].flush()
        self.kwargs["stderr"].write("mock local diagnostic")
        self.kwargs["stderr"].flush()
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


def test_session_control_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENABLE_SESSION_CONTROL_ENV, raising=False)
    result = session.hermes_session_continue("session-1", "hello", hermes_root=tmp_path)
    assert result["success"] is False
    assert result["code"] == "SESSION_CONTROL_DISABLED"


def test_mocked_continue_status_and_result(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "project-manager")
    monkeypatch.setenv(session.op.OPERATOR_ALLOWED_PROFILES_ENV, "project-manager")
    (tmp_path / "profiles" / "project-manager").mkdir(parents=True)
    monkeypatch.setattr(job_runtime_process.threading, "Thread", _ImmediateThread)
    calls = []

    def fake_popen(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(job_runtime_process.subprocess, "Popen", fake_popen)
    prompt = "private follow-up prompt"
    started = session.hermes_session_continue(
        "20260810_143227_6b0982",
        prompt,
        timeout=99999,
        hermes_root=tmp_path,
        agent_root=tmp_path / "agent",
        profile="project-manager",
    )
    assert started["success"] is True
    assert len(calls) == 1
    assert Path(calls[0].argv[0]).name.lower() in {"hermes", "hermes.exe"}
    assert calls[0].argv[1:] == [
        "chat", "--resume", "20260810_143227_6b0982", "--query-file", "-", "--oneshot", "-Q"
    ]
    assert calls[0].kwargs["shell"] is False
    assert calls[0].kwargs["env"]["HERMES_PROFILE"] == "project-manager"
    assert calls[0].kwargs["env"]["HERMES_HOME"] == str(tmp_path / "profiles" / "project-manager")

    status = session.hermes_session_job_status(started["job_id"], tmp_path)
    assert status["job"]["status"] == "completed"
    assert status["job"]["timeout"] == session.MAX_TIMEOUT
    assert status["job"]["profile"] == "project-manager"
    metadata_text = json.dumps(status)
    assert prompt not in metadata_text
    assert status["job"]["prompt_len"] == len(prompt)

    result = session.hermes_session_job_result(started["job_id"], 500, tmp_path)
    assert result["status"] == "completed"
    assert result["return_code"] == 0
    assert "secret-value" not in result["response"]
    assert "[REDACTED]" in result["response"]
    assert "mock local diagnostic" not in result["response"]


def test_continue_accepts_model_and_reasoning_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "default")
    monkeypatch.setenv(session.op.OPERATOR_ALLOWED_PROFILES_ENV, "default")
    (tmp_path / "profiles" / "default").mkdir(parents=True)
    calls = []

    def fake_popen(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(job_runtime_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(job_runtime_process.threading, "Thread", _ImmediateThread)
    result = session.hermes_session_continue(
        "session-2",
        "do one turn",
        model="deepseek/deepseek-v4.1-flash",
        reasoning_effort="xhigh",
        hermes_root=tmp_path,
    )

    assert result["success"] is True
    assert calls[0].argv[1:8] == [
        "chat", "--resume", "session-2", "--model", "deepseek/deepseek-v4.1-flash", "--reasoning", "xhigh"
    ]
    assert "do one turn" not in calls[0].argv


def test_start_uses_authorized_profile_and_recovers_new_session_id(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "chatgpt")
    monkeypatch.setenv(session.op.OPERATOR_ALLOWED_PROFILES_ENV, "chatgpt")
    profile_home = tmp_path / "profiles" / "chatgpt"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(job_runtime_process.threading, "Thread", _ImmediateThread)
    calls = []

    class _NewSessionProcess(_FakeProcess):
        def wait(self, timeout=None):
            self.kwargs["stdout"].write("started safely")
            self.kwargs["stdout"].flush()
            self.kwargs["stderr"].write(
                "session_id: 20260928_120000_ab12cd\n"
            )
            self.kwargs["stderr"].flush()
            self.returncode = 0
            return 0

    def fake_popen(argv, **kwargs):
        proc = _NewSessionProcess(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(job_runtime_process.subprocess, "Popen", fake_popen)
    prompt = "Reply exactly SESSION_STARTED."
    started = session.hermes_session_start(
        prompt,
        hermes_root=tmp_path,
        agent_root=tmp_path / "agent",
        profile="chatgpt",
        model="opencode-go/deepseek-v4.1-flash",
        reasoning_effort="high",
    )

    assert started["success"] is True
    assert len(calls) == 1
    assert calls[0].argv[1:] == [
        "chat",
        "--model",
        "opencode-go/deepseek-v4.1-flash",
        "--reasoning",
        "high",
        "--query-file",
        "-",
        "--oneshot",
        "-Q",
    ]
    assert prompt not in calls[0].argv
    assert calls[0].kwargs["shell"] is False
    assert calls[0].kwargs["cwd"] == str(profile_home)
    assert calls[0].kwargs["env"]["HERMES_PROFILE"] == "chatgpt"
    assert calls[0].kwargs["env"]["HERMES_HOME"] == str(profile_home)

    status = session.hermes_session_job_status(started["job_id"], tmp_path)
    assert status["job"]["status"] == "completed"
    assert status["job"]["session_id"] == "20260928_120000_ab12cd"
    assert status["job"]["profile"] == "chatgpt"
    assert prompt not in json.dumps(status)


def test_start_rejects_unauthorized_profiles_and_invalid_overrides(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "chatgpt")
    monkeypatch.setenv(session.op.OPERATOR_ALLOWED_PROFILES_ENV, "chatgpt")
    (tmp_path / "profiles" / "chatgpt").mkdir(parents=True)

    denied = session.hermes_session_start(
        "start", hermes_root=tmp_path, profile="other"
    )
    invalid_model = session.hermes_session_start(
        "start", hermes_root=tmp_path, profile="chatgpt", model="bad model"
    )
    invalid_effort = session.hermes_session_start(
        "start",
        hermes_root=tmp_path,
        profile="chatgpt",
        reasoning_effort="extreme",
    )

    assert denied["code"] == "SESSION_PROFILE_NOT_ALLOWED"
    assert invalid_model["code"] == "INVALID_MODEL"
    assert invalid_effort["code"] == "INVALID_REASONING_EFFORT"
    assert not (tmp_path / "session-jobs").exists()


def test_job_lookup_and_input_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    assert session.hermes_session_job_status("not-a-job", tmp_path)["code"] == "JOB_NOT_FOUND"
    assert session.hermes_session_continue("s", "", hermes_root=tmp_path)["code"] == "INVALID_PROMPT"
    assert session.hermes_session_continue(
        "s", "x" * (session.MAX_PROMPT_CHARS + 1), hermes_root=tmp_path
    )["code"] == "PROMPT_TOO_LARGE"
    assert session.hermes_session_continue("s", "x", timeout=True, hermes_root=tmp_path)["code"] == "INVALID_TIMEOUT"


def test_scoped_job_recovers_session_id_from_hermes_stderr(tmp_path):
    job_id = "a" * 32
    output_path = job_store._paths(job_id, tmp_path)[1]
    stderr_path = output_path.with_suffix(".stderr.txt")
    stderr_path.parent.mkdir(parents=True)
    stderr_path.write_text("session_id: 20260927_203010_ab12cd\n", encoding="utf-8")
    job_store._save({
        "job_id": job_id,
        "task_id": "b" * 32,
        "session_id": "",
        "active_key": "task:" + "b" * 32,
        "status": "completed",
        "profile": "default",
    }, tmp_path)

    result = session.hermes_session_job_status(job_id, tmp_path)

    assert result["job"]["session_id"] == "20260927_203010_ab12cd"
    assert "active_key" not in result["job"]


def test_scoped_job_recovers_legacy_usage_report_after_stderr_miss(tmp_path):
    job_id = "c" * 32
    usage_file = tmp_path / "profiles" / "task" / "usage.json"
    usage_file.parent.mkdir(parents=True)
    usage_file.write_text(json.dumps({"session_id": "20260927_203010_ab12cd"}), encoding="utf-8")
    job_store._save({
        "job_id": job_id,
        "task_id": "d" * 32,
        "session_id": "",
        "usage_file": str(usage_file),
        "status": "completed",
        "profile": "default",
    }, tmp_path)

    result = session.hermes_session_job_status(job_id, tmp_path)

    assert result["job"]["session_id"] == "20260927_203010_ab12cd"
    assert "usage_file" not in result["job"]


def test_same_session_cannot_run_concurrently(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "default")
    monkeypatch.setitem(
        job_runtime_process._active_sessions, "default:session-1", "b" * 32
    )
    result = session.hermes_session_continue("session-1", "next", hermes_root=tmp_path)
    assert result["code"] == "SESSION_BUSY"


def test_default_profile_requires_explicit_session_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.delenv(session.SESSION_ALLOWED_PROFILES_ENV, raising=False)

    result = session.hermes_session_continue("session-1", "next", hermes_root=tmp_path)

    assert result["success"] is False
    assert result["code"] == "SESSION_PROFILE_NOT_ALLOWED"
    assert not (tmp_path / "session-jobs").exists()


def test_session_profile_allowlist_rejects_wildcards(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "*")

    result = session.hermes_session_continue("session-1", "next", hermes_root=tmp_path)

    assert result["success"] is False
    assert result["code"] == "SESSION_PROFILE_ALLOWLIST_INVALID"


def test_named_session_profile_uses_configured_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "project-manager")
    monkeypatch.setenv(session.op.OPERATOR_ALLOWED_PROFILES_ENV, "project-manager")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "profiles" / "project-manager").mkdir(parents=True)

    result = session.validate_session_profile("project-manager")

    assert result == "project-manager"


def test_session_profile_must_also_pass_operator_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "project-manager")
    monkeypatch.setenv(session.op.OPERATOR_ALLOWED_PROFILES_ENV, "default")
    (tmp_path / "profiles" / "project-manager").mkdir(parents=True)

    result = session.hermes_session_continue(
        "session-1", "next", hermes_root=tmp_path, profile="project-manager"
    )

    assert result["success"] is False
    assert result["code"] == "SESSION_PROFILE_NOT_ALLOWED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cancellation")
def test_cancel_stops_owned_process_group_and_persists_status(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, "default")
    monkeypatch.setattr(job_runtime_process, "PROGRESS_EVENT_INTERVAL_SECONDS", 0.1)
    agent_root = tmp_path / "agent"
    executable = agent_root / "venv" / "bin" / "hermes"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "import time\n"
        "print('diagnostic-only', file=sys.stderr, flush=True)\n"
        "print('session-started', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    started = session.hermes_session_continue(
        "session-cancel-test",
        "run the local cancellation fixture",
        hermes_root=tmp_path,
        agent_root=agent_root,
    )
    assert started["success"] is True
    job_id = started["job_id"]
    job_meta = job_store._load(job_id, tmp_path)
    assert job_meta is not None
    pid = job_meta["pid"]

    deadline = time.monotonic() + 3
    output_path = job_store._paths(job_id, tmp_path)[1]
    while time.monotonic() < deadline:
        if output_path.exists() and "session-started" in output_path.read_text(encoding="utf-8"):
            break
        time.sleep(0.02)
    else:
        pytest.fail("Hermes cancellation fixture did not start")

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        events, _ = live_events.read_since(
            0,
            topic=job_runtime.SESSION_JOB_TOPIC,
            hermes_root=tmp_path,
        )
        if any(event["kind"] == "progress" for event in events):
            break
        time.sleep(0.02)
    else:
        pytest.fail("The running session job did not publish a progress event")

    cancelled = session.hermes_session_job_cancel(job_id, tmp_path)
    assert cancelled["success"] is True
    assert cancelled["cancelled"] is True
    assert cancelled["status"] == "cancelled"

    status = session.hermes_session_job_status(job_id, tmp_path)
    result = session.hermes_session_job_result(job_id, hermes_root=tmp_path)
    assert status["job"]["status"] == "cancelled"
    assert result["status"] == "cancelled"
    stderr_path = output_path.with_suffix(".stderr.txt")
    assert "diagnostic-only" in stderr_path.read_text(encoding="utf-8")
    assert "diagnostic-only" not in result["response"]
    events, _ = live_events.read_since(
        0,
        topic=job_runtime.SESSION_JOB_TOPIC,
        hermes_root=tmp_path,
    )
    event_kinds = [event["kind"] for event in events]
    assert event_kinds[0] == "running"
    assert "progress" in event_kinds
    assert event_kinds[-1] == "cancelled"
    assert all("run the local cancellation fixture" not in str(event["payload"]) for event in events)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    repeated = session.hermes_session_job_cancel(job_id, tmp_path)
    assert repeated["success"] is True
    assert repeated["cancelled"] is True


def test_cancel_marks_unowned_job_orphaned_without_signaling_pid(monkeypatch, tmp_path):
    job_id = "c" * 32
    job_store._save(
        {"job_id": job_id, "session_id": "session-1", "status": "running", "pid": 999999},
        tmp_path,
    )
    monkeypatch.setattr(
        job_runtime,
        "_terminate",
        lambda _proc: pytest.fail("cancel must not signal a persisted PID"),
    )

    result = session.hermes_session_job_cancel(job_id, tmp_path)

    assert result["success"] is True
    assert result["status"] == "orphaned"
    assert result["cancelled"] is False
    events, _ = live_events.read_since(
        0,
        topic=job_runtime.SESSION_JOB_TOPIC,
        hermes_root=tmp_path,
    )
    assert [event["kind"] for event in events] == ["orphaned"]


def test_reconcile_marks_unowned_running_job_orphaned(tmp_path):
    job_id = "a" * 32
    job_store._save({"job_id": job_id, "session_id": "s", "status": "running"}, tmp_path)
    result = session.hermes_session_job_status(job_id, tmp_path)
    assert result["job"]["status"] == "orphaned"
    assert "ownership" in result["job"]["reconciliation"]
