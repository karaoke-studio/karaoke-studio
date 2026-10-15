from __future__ import annotations

"""Public updater API; build metadata must not eagerly import GUI dependencies."""

from importlib import import_module

__all__ = [
    "CheckResult",
    "UpdateChecker",
    "UpdaterSettings",
    "ensure_updater_settings",
]

_EXPORT_MODULES = {
    "CheckResult": "worker",
    "UpdateChecker": "worker",
    "UpdaterSettings": "settings",
    "ensure_updater_settings": "settings",
}


def __getattr__(name: str):
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
