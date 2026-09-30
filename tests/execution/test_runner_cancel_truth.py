import operator_runners as runners


def _seed_meta(tmp_path, task_id: str, *, state: str = "running"):
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    runners._atomic_json(
        meta_path,
        {
            "schema_version": runners.SCHEMA_VERSION,
            "task_id": task_id,
            "backend": "omx",
            "state": state,
            "outcome": state if state != "running" else "",
            "workspace": str(tmp_path),
            "created_at": runners._now(),
            "started_at": runners._now(),
            "ended_at": None,
            "pid": 4242,
            "returncode": None,
            "error": "",
        },
    )
    return meta_path


def test_local_cancel_does_not_publish_marker_before_supervisor_verifies(monkeypatch, tmp_path):
    task_id = "cancel-unverified"
    meta_path = _seed_meta(tmp_path, task_id)
    cancel_path = runners._cancel_path(task_id, tmp_path)

    def refuse(job_id, *, hermes_root=None):
        assert job_id == task_id
        assert hermes_root == tmp_path
        assert not cancel_path.exists()
        return {
            "success": False,
            "changed": False,
            "code": "JOB_PROCESS_UNVERIFIABLE",
            "safe_message": "identity mismatch",
        }

    monkeypatch.setattr(runners.job_supervisor, "request_cancel", refuse)
    result = runners.OmxBackend().cancel(task_id, hermes_root=tmp_path)

    assert result["success"] is False
    assert result["code"] == "JOB_PROCESS_UNVERIFIABLE"
    assert not cancel_path.exists()
    stored = runners._load_json(meta_path)
    assert stored is not None
    assert stored["state"] == "running"


def test_local_cancel_preserves_supervisor_completed_noop(monkeypatch, tmp_path):
    task_id = "cancel-completed-race"
    meta_path = _seed_meta(tmp_path, task_id)

    monkeypatch.setattr(
        runners.job_supervisor,
        "request_cancel",
        lambda job_id, *, hermes_root=None: {
            "success": True,
            "changed": False,
            "job_id": job_id,
            "status": "completed",
        },
    )

    result = runners.OmxBackend().cancel(task_id, hermes_root=tmp_path)

    assert result == {
        "success": True,
        "changed": False,
        "backend": "omx",
        "state": "completed",
    }
    stored = runners._load_json(meta_path)
    assert stored is not None
    assert stored["state"] == "completed"
    assert stored["outcome"] == "completed"
    assert stored["ended_at"]
