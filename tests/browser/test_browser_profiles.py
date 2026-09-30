from __future__ import annotations

import pytest

from hermes_gpt.browser import profiles


@pytest.mark.parametrize(
    ("endpoint", "expected_port"),
    [
        ("http://localhost:9222", 9222),
        ("ws://127.0.0.1:9223/devtools/browser/abc-123", 9223),
        ("[::1]:9224", 9224),
    ],
)
def test_local_browser_endpoints_are_reduced_to_a_port(endpoint, expected_port):
    assert profiles.validate_local_cdp_port(endpoint) == expected_port


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://localhost:9222",
        "ws://browser.example:9222/devtools/browser/abc",
        "http://localhost:9222?token=private-value",
        "http://user:password@localhost:9222",
        "http://localhost:9222/custom/path",
        "http://localhost:99999",
    ],
)
def test_remote_or_credentialed_browser_endpoints_are_rejected(endpoint):
    with pytest.raises(ValueError):
        profiles.validate_local_cdp_port(endpoint)


def test_profile_cdp_port_requires_explicit_profile_allowlist(
    monkeypatch, tmp_path
):
    root = tmp_path / "hermes-home"
    profile_home = root / "profiles" / "browser"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "browser:\n  cdp_url: http://127.0.0.1:9222\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(profiles.BROWSER_ALLOWED_PROFILES_ENV, raising=False)

    with pytest.raises(PermissionError):
        profiles.profile_cdp_port("browser", root)

    monkeypatch.setenv(profiles.BROWSER_ALLOWED_PROFILES_ENV, "browser")
    assert profiles.profile_cdp_port("browser", root) == 9222


def test_profile_cdp_port_rejects_profile_outside_browser_allowlist(
    monkeypatch, tmp_path
):
    root = tmp_path / "hermes-home"
    profile_home = root / "profiles" / "browser"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "browser:\n  cdp_url: http://127.0.0.1:9222\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(profiles.BROWSER_ALLOWED_PROFILES_ENV, "another-profile")

    with pytest.raises(PermissionError):
        profiles.profile_cdp_port("browser", root)
