"""Least-privilege environment for every sidecar child process."""

from __future__ import annotations

import os

_SIDECAR_ONLY_ENV = frozenset({"AISO_AUTH_TOKEN", "AISO_CREDENTIAL_CHANNEL_TOKEN"})

# Code and shell tools inherit a deliberately reduced environment.  The exact
# provider list will keep changing, so combine known credential names with
# conservative secret suffixes rather than relying on a short one-off list.
_CREDENTIAL_ENV_NAMES = frozenset({
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "NVIDIA_API_KEY",
    "NGC_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "DISCORD_TOKEN",
    "NPM_TOKEN",
    "NODE_AUTH_TOKEN",
    "PYPI_TOKEN",
    "DOCKER_AUTH_CONFIG",
    "DATABASE_URL",
    "REDIS_URL",
    "MONGODB_URI",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
})
_CREDENTIAL_ENV_SUFFIXES = (
    "_TOKEN",
    "_SECRET",
    "_KEY",
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_KEY",
    "_SECRET_KEY",
    "_SESSION_TOKEN",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_CLIENT_SECRET",
    "_PRIVATE_KEY",
    "_PASSWORD",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_CONNECTION_STRING",
)


def _is_sensitive_environment_name(name: str) -> bool:
    upper = name.upper()
    return (
        upper in _SIDECAR_ONLY_ENV
        or upper in _CREDENTIAL_ENV_NAMES
        or upper.endswith(_CREDENTIAL_ENV_SUFFIXES)
    )


def sanitized_child_environment(**extra: str) -> dict[str, str]:
    env = {**os.environ, **extra}
    return {
        name: value
        for name, value in env.items()
        if not _is_sensitive_environment_name(name)
    }
