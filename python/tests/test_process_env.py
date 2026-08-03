from __future__ import annotations

from process_env import sanitized_child_environment


def test_all_sidecar_child_processes_drop_both_authentication_channels(monkeypatch):
    monkeypatch.setenv("AISO_AUTH_TOKEN", "ordinary-canary")
    monkeypatch.setenv("AISO_CREDENTIAL_CHANNEL_TOKEN", "credential-canary")
    env = sanitized_child_environment(PYTHONUTF8="1")
    assert "AISO_AUTH_TOKEN" not in env
    assert "AISO_CREDENTIAL_CHANNEL_TOKEN" not in env
    assert env["PYTHONUTF8"] == "1"


def test_child_process_environment_drops_cloud_and_generic_credentials(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvidia-canary")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-canary")
    monkeypatch.setenv("CUSTOM_SERVICE_ACCESS_TOKEN", "custom-canary")
    monkeypatch.setenv("PROJECT_PASSWORD", "password-canary")
    monkeypatch.setenv("AISO_BENIGN_SETTING", "preserved")

    env = sanitized_child_environment()

    assert "NVIDIA_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "CUSTOM_SERVICE_ACCESS_TOKEN" not in env
    assert "PROJECT_PASSWORD" not in env
    assert env["AISO_BENIGN_SETTING"] == "preserved"


def test_child_process_environment_drops_package_registry_and_connection_credentials(monkeypatch):
    sensitive = {
        "NPM_TOKEN": "npm-canary",
        "PYPI_TOKEN": "pypi-canary",
        "DOCKER_AUTH_CONFIG": "docker-canary",
        "DATABASE_URL": "postgres://secret-canary",
        "CUSTOM_API_TOKEN": "api-token-canary",
        "CUSTOM_SECRET": "secret-canary",
        "CUSTOM_CREDENTIALS": "credentials-canary",
        "CUSTOM_CONNECTION_STRING": "connection-canary",
    }
    for name, value in sensitive.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AISO_VISIBLE_LOCALE", "ko-KR")

    env = sanitized_child_environment()

    assert sensitive.keys().isdisjoint(env)
    assert env["AISO_VISIBLE_LOCALE"] == "ko-KR"
