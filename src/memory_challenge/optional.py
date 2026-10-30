"""Optional heavyweight dependencies: imported only when a config enables them, with an actionable error otherwise."""
from __future__ import annotations

import importlib


class OptionalDependencyError(RuntimeError):
    pass


def require(module: str, feature: str, extra: str = "dense"):
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise OptionalDependencyError(
            f"{feature} needs the '{module}' package, which is not installed. Install the optional extra in this "
            f"project's virtual environment (python -m venv .venv && .venv/bin/pip install -e '.[{extra}]') or turn "
            f"the feature off in the config.") from exc
