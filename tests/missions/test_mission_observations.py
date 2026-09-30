"""Malformed child evidence must remain unverified and fail closed."""

import json

import pytest

from hermes_gpt.execution import delegations
from hermes_gpt.missions import observations


@pytest.mark.parametrize("payload", [[], None, {"status": []}, {"status": {}}])
def test_workflow_observation_rejects_malformed_state(tmp_path, payload):
    directory = tmp_path / "swarm-workflows"
    directory.mkdir()
    (directory / "sw-child.json").write_text(json.dumps(payload), encoding="utf-8")
    assert observations.workflow_state(tmp_path, "sw-child") == "unknown"


@pytest.mark.parametrize("payload", [[], None, {
    "success": True,
    "delegation": {"mission_id": "mission", "authority_version": "invalid"},
}])
def test_delegation_observation_rejects_malformed_state(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(delegations, "hermes_delegation_reconcile",
                        lambda *args, **kwargs: json.dumps(payload))
    assert observations.delegation_state(tmp_path, "mission", {"ref": "child"}) == (
        "blocked", False, None,
    )
