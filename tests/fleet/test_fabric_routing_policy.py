"""Guard the Fabric routing-policy extraction boundary.

The policy models, the shared error type, and policy loading/validation moved to
``operator_fabric_routing_policy``. These tests pin the parts callers depend on:
the one-way import direction, the historical re-exports from the router, and the
fail-closed loader behavior.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_gpt.fleet import fabric_router as router
from hermes_gpt.fleet import fabric_routing_policy as policy

NOW = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)


def imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_policy_module_never_imports_the_router():
    source = Path(policy.__file__).read_text(encoding="utf-8")
    assert "hermes_gpt.fleet.fabric_router" not in imported_modules(source)


def test_router_reexports_policy_names_with_unchanged_identity():
    assert router.RoutingError is policy.RoutingError
    assert router.RoutingPolicy is policy.RoutingPolicy
    assert router.TargetFacts is policy.TargetFacts
    assert router.load_routing_policy is policy.load_routing_policy
    assert router.ROUTING_POLICY_SCHEMA == policy.ROUTING_POLICY_SCHEMA == "hermes.fabric-routing-policy/v1"
    assert router.ROUTING_POLICY_ENV == policy.ROUTING_POLICY_ENV == "HERMES_GPT_FABRIC_ROUTING_POLICY"
    for name in (
        "_safe_text", "_tokens", "_iso_datetime", "_bucket", "_count",
        "_bool_or_none", "_routing_policy_path", "_MAX_AGE_SECONDS",
        "_MAX_LIST", "_MAX_TARGETS", "_TOKEN_RE", "_TARGET_RE",
    ):
        assert getattr(router, name) is getattr(policy, name)


def test_router_error_handling_catches_policy_errors(tmp_path, monkeypatch):
    monkeypatch.setenv(router.ROUTING_POLICY_ENV, str(tmp_path / "missing-routing.json"))
    with pytest.raises(policy.RoutingError) as exc:
        router.load_routing_policy()
    assert exc.value.code == "FABRIC_ROUTING_CONFIG_MISSING"


def test_target_facts_freshness_window_is_unchanged():
    facts = policy.TargetFacts(
        observed_at=NOW,
        max_age_seconds=300,
        os_names=frozenset({"linux"}),
        runtimes=frozenset(),
        runners=frozenset(),
        providers=frozenset(),
        models=frozenset(),
        tools=frozenset(),
        browser=None,
        vision=None,
        gpu_available=None,
        gpu_vendor="",
        gpu_memory_mb=None,
        capacity=None,
        active=None,
        cost_bucket=None,
        locality_bucket=None,
    )
    assert facts.fresh(NOW + timedelta(seconds=300)) is True
    assert facts.fresh(NOW + timedelta(seconds=301)) is False
    assert facts.fresh(NOW - timedelta(seconds=60)) is True
    assert facts.fresh(NOW - timedelta(seconds=61)) is False


def test_default_router_loader_honours_a_router_module_patch(monkeypatch):
    calls: list[dict[str, object]] = []

    def stub_loader(**kwargs):
        calls.append(kwargs)
        return router.RoutingPolicy(targets={})

    monkeypatch.setattr(router, "load_routing_policy", stub_loader)
    auto = router.AutoRouter(
        registry_loader=dict,
        local_backends=list,
        local_posture=lambda _dry: {"ready": False, "max_authorization": "none"},
        now=lambda: NOW,
    )
    decision = auto.route(
        {
            "task_id": "task-auto-1",
            "assigned_agent": "auto",
            "authorization": {"class": "read_only"},
            "execution": {"backend": "auto", "options": {}},
        },
        dry_run=True,
    )
    assert calls == [{"hermes_root": None}]
    assert decision["selected"] is None
    assert decision["candidates"] == []
