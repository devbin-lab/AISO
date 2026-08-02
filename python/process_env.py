"""Least-privilege environment for every sidecar child process."""

from __future__ import annotations

import os

_SIDECAR_ONLY_ENV = ("AISO_AUTH_TOKEN", "AISO_CREDENTIAL_CHANNEL_TOKEN")


def sanitized_child_environment(**extra: str) -> dict[str, str]:
    env = {**os.environ, **extra}
    for name in _SIDECAR_ONLY_ENV:
        env.pop(name, None)
    return env
