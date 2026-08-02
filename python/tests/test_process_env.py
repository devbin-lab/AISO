from __future__ import annotations

from process_env import sanitized_child_environment


def test_all_sidecar_child_processes_drop_both_authentication_channels(monkeypatch):
    monkeypatch.setenv("AISO_AUTH_TOKEN", "ordinary-canary")
    monkeypatch.setenv("AISO_CREDENTIAL_CHANNEL_TOKEN", "credential-canary")
    env = sanitized_child_environment(PYTHONUTF8="1")
    assert "AISO_AUTH_TOKEN" not in env
    assert "AISO_CREDENTIAL_CHANNEL_TOKEN" not in env
    assert env["PYTHONUTF8"] == "1"
