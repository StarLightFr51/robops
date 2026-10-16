"""Windows installer for robops (compiled into robops-setup.exe).

  robops-setup.exe               installs or updates to the latest version
  robops-setup.exe --force       reinstalls even if the version is up to date
  robops-setup.exe --uninstall   uninstalls (also launched from "Apps & features")

Installs into %LOCALAPPDATA%\\Programs\\robops (no administrator rights needed),
adds it to the user's PATH and registers it in "Apps & features".
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests

from . import updater, winsetup
from .output import ensure_utf8_output


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _launched_by_double_click() -> bool:
    """True if the console was created for us (double-click): we wait before closing it."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        processes = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) <= 1
    except Exception:
        return False


def install(install_dir: Path, *, force: bool = False, session: requests.Session | None = None,
            reg: Any = None) -> int:
    repo = updater.repository()
    session = session or updater.make_session(updater.github_token())

    _say(f"Looking for the latest version on github.com/{repo} …")
    release = updater.latest_release(repo, session=session)
    _say(f"  latest published version: {release.version}")

    exe = install_dir / updater.EXE_NAME
    current = winsetup.installed_version(exe)
    if current and not force and not updater.is_newer(release.version, current):
        _say(f"✔ robops {current} is already installed and up to date.")
    else:
        action = f"Updating {current} → {release.version}" if current else f"Installing {release.version}"
        _say(f"{action} in {install_dir} …")
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=install_dir) as tmp:
            new_file = updater.fetch_verified(release, updater.EXE_NAME, Path(tmp), session=session)
            _say("  SHA-256 checksum verified")
            updater.replace_executable(new_file, exe)
        updater.cleanup_old(exe)
        _say(f"✔ robops {release.version} installed")

    # Copy of the installer next to the tool: used for uninstalling.
    setup_copy = install_dir / updater.SETUP_NAME
    if updater.is_frozen():
        me = updater.current_executable()
        if me != setup_copy.resolve():
            try:
                shutil.copy2(me, setup_copy)
            except OSError:
                pass

    if winsetup.add_to_user_path(install_dir, reg):
        _say("✔ Folder added to your Windows account's PATH")
        path_note = "Open a NEW terminal (windows that are already open don't see the updated PATH)"
    else:
        path_note = "Open a terminal"
    winsetup.register_uninstall(install_dir, release.version, setup_copy, reg)

    _say()
    _say(f"{path_note}, then type:  robops --version")
    _say("Later updates will install automatically (checked once a day).")
    return 0


def install_local(exe: Path, install_dir: Path, *, setup_exe: Path | None = None,
                  version: str = "", reg: Any = None) -> bool:
    """Installs a robops.exe compiled on this PC (exe/build.py --install).

    Returns True if the folder was just added to the PATH.
    """
    if not exe.is_file():
        raise OSError(f"Executable not found: {exe}")
    install_dir.mkdir(parents=True, exist_ok=True)
    target = install_dir / updater.EXE_NAME
    staged = install_dir / ".robops-new.exe"
    shutil.copy2(exe, staged)
    updater.replace_executable(staged, target)      # works even if robops is already running
    updater.cleanup_old(target)
    if setup_exe is not None and setup_exe.is_file():
        shutil.copy2(setup_exe, install_dir / updater.SETUP_NAME)
        winsetup.register_uninstall(install_dir, version, install_dir / updater.SETUP_NAME, reg)
    return winsetup.add_to_user_path(install_dir, reg)


def uninstall(install_dir: Path, *, reg: Any = None) -> int:
    _say(f"Uninstalling robops ({install_dir}) …")
    if winsetup.remove_from_user_path(install_dir, reg):
        _say("✔ Removed from PATH")
    winsetup.unregister_uninstall(reg)
    shutil.rmtree(updater.state_dir(), ignore_errors=True)

    running_inside = False
    if updater.is_frozen():
        try:
            updater.current_executable().relative_to(install_dir.resolve())
            running_inside = True
        except ValueError:
            pass

    if running_inside and os.name == "nt":
        # A running .exe cannot delete itself:
        # we hand the folder deletion over to cmd, a few seconds after we exit.
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["cmd", "/c", f'ping 127.0.0.1 -n 4 > nul & rmdir /s /q "{install_dir}"'],
            creationflags=flags, close_fds=True,
        )
    else:
        shutil.rmtree(install_dir, ignore_errors=True)
    _say("✔ robops has been uninstalled.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_output()
    parser = argparse.ArgumentParser(
        prog="robops-setup",
        description="Installs, updates or uninstalls robops from the GitHub releases.",
    )
    parser.add_argument("--uninstall", action="store_true", help="uninstall robops")
    parser.add_argument("--force", action="store_true", help="reinstall even if the version is up to date")
    parser.add_argument("--dir", type=Path, help="install folder (default: %%LOCALAPPDATA%%\\Programs\\robops)")
    parser.add_argument("--quiet", action="store_true", help="do not wait for Enter at the end")
    args = parser.parse_args(argv)

    install_dir = (args.dir or winsetup.default_install_dir()).expanduser()
    try:
        if args.uninstall:
            code = uninstall(install_dir)
        else:
            if os.name != "nt":
                _say("⚠ This installer is designed for Windows (PATH via the user registry).")
                return 1
            code = install(install_dir, force=args.force)
    except updater.UpdateError as exc:
        _say(f"✖ {exc}")
        code = 1
    except OSError as exc:
        _say(f"✖ File error: {exc}")
        code = 1

    if not args.quiet and _launched_by_double_click():
        try:
            input("\nPress Enter to close this window…")
        except EOFError:
            pass
    return code
