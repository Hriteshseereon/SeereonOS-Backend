"""Process-wide singletons of the running API (set by app.main.create_app): the runtime and its configuration."""

from __future__ import annotations

from typing import Any

RUNTIME: Any = None


def runtime() -> Any:
    if RUNTIME is None:
        raise RuntimeError('the API runtime is not initialised')
    return RUNTIME
