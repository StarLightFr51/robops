"""Windows integration: user PATH and "Apps & features".

Everything happens in HKEY_CURRENT_USER: no administrator rights are required.
The `winreg` module is injectable so this code can be tested outside Windows.
"""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
from pathlib import Path
from typing import Any

ENV_KEY = "Environment"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\robops"


def _winreg(reg: Any = None) -> Any:
    if reg is not None:
        return reg
    import winreg  # only available on Windows
    return winreg


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "robops"


def _same_dir(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        return ntpath.normcase(ntpath.normpath(os.path.expandvars(p.strip().strip('"')))).rstrip("\\")
    return norm(a) == norm(b)


# ---------------------------------------------------------------- PATH

def read_user_path(reg: Any = None) -> tuple[str, int]:
    winreg = _winreg(reg)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENV_KEY, 0, winreg.KEY_READ) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
            return str(value or ""), kind
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ


def _write_user_path(value: str, kind: int, reg: Any = None) -> None:
    winreg = _winreg(reg)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENV_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, value)
    broadcast_environment_change()


def add_to_user_path(directory: Path, reg: Any = None) -> bool:
    """Adds the folder to the user's PATH. Returns False if it was already there."""
    current, kind = read_user_path(reg)
    parts = [p for p in current.split(";") if p.strip()]
    if any(_same_dir(p, str(directory)) for p in parts):
        return False
    parts.append(str(directory))
    _write_user_path(";".join(parts), kind, reg)
    return True


def remove_from_user_path(directory: Path, reg: Any = None) -> bool:
    current, kind = read_user_path(reg)
    parts = [p for p in current.split(";") if p.strip()]
    kept = [p for p in parts if not _same_dir(p, str(directory))]
    if len(kept) == len(parts):
        return False
    _write_user_path(";".join(kept), kind, reg)
    return True


def broadcast_environment_change() -> None:
    """Tells Windows the PATH changed (new terminals will see it)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        result = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
        )
    except Exception:  # purely cosmetic: must never make the installation fail
        pass


# ------------------------------------------------- Apps & features

def register_uninstall(install_dir: Path, version: str, setup_exe: Path, reg: Any = None) -> None:
    winreg = _winreg(reg)
    exe = install_dir / "robops.exe"
    size_kb = sum(f.stat().st_size for f in install_dir.glob("*.exe") if f.is_file()) // 1024
    values = {
        "DisplayName": (winreg.REG_SZ, "robops"),
        "DisplayVersion": (winreg.REG_SZ, version),
        "Publisher": (winreg.REG_SZ, "robops"),
        "InstallLocation": (winreg.REG_SZ, str(install_dir)),
        "DisplayIcon": (winreg.REG_SZ, str(exe)),
        "UninstallString": (winreg.REG_SZ, f'"{setup_exe}" --uninstall'),
        "QuietUninstallString": (winreg.REG_SZ, f'"{setup_exe}" --uninstall --quiet'),
        "NoModify": (winreg.REG_DWORD, 1),
        "NoRepair": (winreg.REG_DWORD, 1),
        "EstimatedSize": (winreg.REG_DWORD, int(size_kb)),
    }
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, (kind, value) in values.items():
            winreg.SetValueEx(key, name, 0, kind, value)


def unregister_uninstall(reg: Any = None) -> None:
    winreg = _winreg(reg)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- misc

def installed_version(exe: Path) -> str | None:
    """Version of an already installed robops.exe (None if missing or unreadable)."""
    if not exe.is_file():
        return None
    try:
        completed = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=60,
            env={**os.environ, "ROBOPS_NO_UPDATE": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+(?:\.\d+)+)", completed.stdout + completed.stderr)
    return match.group(1) if match else None
