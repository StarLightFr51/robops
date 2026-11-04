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
import tempfile
from pathlib import Path
from typing import Any

import requests

from . import updater, winsetup
from .output import ensure_utf8_output


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _is_windows() -> bool:
    return os.name == "nt"


def _launched_by_double_click() -> bool:
    """True if the console was created for us (double-click): we wait before closing it."""
    if not _is_windows():
        return False
    try:
        import ctypes
        processes = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) <= 1  # type: ignore[attr-defined,unused-ignore]
    except Exception:  # noqa: BLE001 — cosmetic, must never fail
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
    _say("robops checks for new versions once a day and tells you; install them with:  robops self-update")
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


def _installed_files(install_dir: Path) -> list[Path]:
    """Files that robops itself puts in its folder: the only ones uninstalling deletes."""
    names = (updater.EXE_NAME, updater.SETUP_NAME)
    found = [install_dir / name for name in names]
    for name in names:
        found += install_dir.glob(name + ".old*")               # left behind by an update
    found += install_dir.glob(".robops-new.exe")                # exe/build.py --install
    found += install_dir.glob(".download-*")                    # interrupted install
    found += install_dir.glob(".robops-update-*")               # interrupted update
    return [p for p in found if p.exists()]


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


def _delete_after_exit(files: list[Path], install_dir: Path) -> None:
    """A running .exe cannot delete itself: cmd does it a few seconds after we exit.

    The command line is passed as a string: given a list, Python would escape the
    quotes as \\" (C runtime rules), which cmd.exe does not understand.
    """
    deletions = " & ".join(f'del /f /q "{f}"' for f in files)
    # rmdir without /s: the folder only disappears if it is empty (user files are kept).
    command = f'cmd /c ping 127.0.0.1 -n 4 > nul & {deletions} & rmdir "{install_dir}"'
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(command, creationflags=flags, close_fds=True)


def uninstall(install_dir: Path, *, reg: Any = None) -> int:
    _say(f"Uninstalling robops ({install_dir}) …")
    if winsetup.remove_from_user_path(install_dir, reg):
        _say("✔ Removed from PATH")
    winsetup.unregister_uninstall(reg)
    state = updater.state_dir()
    (state / "update.json").unlink(missing_ok=True)
    try:
        state.rmdir()
    except OSError:
        pass

    me = updater.current_executable() if updater.is_frozen() else None
    later: list[Path] = []
    for path in _installed_files(install_dir):
        if me is not None and _is_windows() and path.resolve() == me:
            later.append(path)
        else:
            _remove(path)

    stuck = [p for p in _installed_files(install_dir) if p not in later]
    if later:
        _delete_after_exit(later, install_dir)
    elif not stuck:
        try:
            install_dir.rmdir()          # only if empty: never delete files we did not create
        except OSError:
            pass
    if stuck:
        _say(f"⚠ Could not delete {', '.join(p.name for p in stuck)} (robops still running?): "
             "close it and run the uninstall again.")
    elif install_dir.exists() and not later:
        _say(f"  {install_dir} kept: it contains files that robops did not create.")
    _say("✔ robops has been uninstalled.")
    return 0


def _default_install_dir() -> Path:
    """Folder to act on without --dir: the one of this setup if it is an installed copy
    (uninstall from "Apps & features" of an older version), otherwise the default."""
    if updater.is_frozen():
        me = updater.current_executable()
        if me.name.lower() == updater.SETUP_NAME and (me.parent / updater.EXE_NAME).is_file():
            return me.parent
    return winsetup.default_install_dir()


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

    install_dir = (args.dir or _default_install_dir()).expanduser()
    try:
        if args.uninstall:
            code = uninstall(install_dir)
        else:
            if not _is_windows():
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
