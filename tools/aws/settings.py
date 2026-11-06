"""Private integration settings shared with the memory bootstrap.

Only the instance's owner-private .env is consulted; ambient AWS defaults and
legacy tracked configuration files are never alternate sources. Resource names
and profile selection are required, while credentials remain in their stores.
"""

import re

from tools.memory.environment import MemoryClientError, load_environment
from tools.project import default_environment


ENVIRONMENT = default_environment()


def load_settings() -> dict[str, str]:
    return load_environment(ENVIRONMENT)


def required(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise MemoryClientError("integration_setting_required")
    return value


def prefix(values: dict[str, str], key: str, *, parameter: bool = True) -> str:
    value = required(values, key)
    pattern = r"/(?:[A-Za-z0-9_.-]+/)+" if parameter else r"(?:[A-Za-z0-9_.-]+/)+"
    if not re.fullmatch(pattern, value) or any(part in (".", "..") for part in value.split("/")):
        raise MemoryClientError("invalid_integration_prefix")
    return value
