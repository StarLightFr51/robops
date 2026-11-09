"""`requests`, imported the first time it is used rather than when robops starts.

requests (with urllib3, idna, charset_normalizer… that it pulls in) takes about
90 ms to import. The commands that don't talk to Roblox or GitHub (--help,
check, init, mcp serve, mcp status…) start that much faster without it.

Modules write, so that type checkers still see the real module:

    if TYPE_CHECKING:
        import requests
    else:
        from ._lazy import requests
"""

from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any

_lock = threading.Lock()


class _LoadOnFirstUse:
    def __init__(self, name: str):
        self._name = name
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            with _lock:                       # dashboard threads may be the first to use it
                if self._module is None:
                    self._module = importlib.import_module(self._name)
        return self._module

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._load(), attr)

    def __repr__(self) -> str:
        return f"<{self._name}, imported on first use>"


requests = _LoadOnFirstUse("requests")
