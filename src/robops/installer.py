"""Installateur Windows de robops (compilé en robops-setup.exe).

  robops-setup.exe               installe ou met à jour la dernière version
  robops-setup.exe --force       réinstalle même si la version est à jour
  robops-setup.exe --uninstall   désinstalle (aussi lancé depuis « Applications et fonctionnalités »)

Installation dans %LOCALAPPDATA%\\Programs\\robops (sans droits administrateur),
ajout au PATH de l'utilisateur et enregistrement dans « Applications et fonctionnalités ».
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
    """Vrai si la console a été créée pour nous (double-clic) : on attend avant de la fermer."""
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
    if not repo:
        _say("✖ Cet installateur a été compilé sans dépôt GitHub (voir release.yml).")
        return 1
    session = session or updater.make_session(updater.github_token())

    _say(f"Recherche de la dernière version sur github.com/{repo} …")
    release = updater.latest_release(repo, session=session)
    _say(f"  dernière version publiée : {release.version}")

    exe = install_dir / updater.EXE_NAME
    current = winsetup.installed_version(exe)
    if current and not force and not updater.is_newer(release.version, current):
        _say(f"✔ robops {current} est déjà installé et à jour.")
    else:
        action = f"Mise à jour {current} → {release.version}" if current else f"Installation de {release.version}"
        _say(f"{action} dans {install_dir} …")
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=install_dir) as tmp:
            new_file = updater.fetch_verified(release, updater.EXE_NAME, Path(tmp), session=session)
            _say("  empreinte SHA-256 vérifiée")
            updater.replace_executable(new_file, exe)
        updater.cleanup_old(exe)
        _say(f"✔ robops {release.version} installé")

    # Copie de l'installateur à côté de l'outil : sert à la désinstallation.
    setup_copy = install_dir / updater.SETUP_NAME
    if updater.is_frozen():
        me = updater.current_executable()
        if me != setup_copy.resolve():
            try:
                shutil.copy2(me, setup_copy)
            except OSError:
                pass

    if winsetup.add_to_user_path(install_dir, reg):
        _say("✔ Dossier ajouté au PATH de ton compte Windows")
        path_note = "Ouvre un NOUVEAU terminal (les fenêtres déjà ouvertes ne voient pas le PATH mis à jour)"
    else:
        path_note = "Ouvre un terminal"
    winsetup.register_uninstall(install_dir, release.version, setup_copy, reg)

    _say()
    _say(f"{path_note}, puis tape :  robops --version")
    _say("Les mises à jour suivantes s'installeront automatiquement (vérification une fois par jour).")
    return 0


def install_local(exe: Path, install_dir: Path, *, setup_exe: Path | None = None,
                  version: str = "", reg: Any = None) -> bool:
    """Installe un robops.exe compilé sur ce PC (exe/build.py --install).

    Retourne True si le dossier vient d'être ajouté au PATH.
    """
    if not exe.is_file():
        raise OSError(f"Exécutable introuvable : {exe}")
    install_dir.mkdir(parents=True, exist_ok=True)
    target = install_dir / updater.EXE_NAME
    staged = install_dir / ".robops-new.exe"
    shutil.copy2(exe, staged)
    updater.replace_executable(staged, target)      # fonctionne même si robops tourne déjà
    updater.cleanup_old(target)
    if setup_exe is not None and setup_exe.is_file():
        shutil.copy2(setup_exe, install_dir / updater.SETUP_NAME)
        winsetup.register_uninstall(install_dir, version, install_dir / updater.SETUP_NAME, reg)
    return winsetup.add_to_user_path(install_dir, reg)


def uninstall(install_dir: Path, *, reg: Any = None) -> int:
    _say(f"Désinstallation de robops ({install_dir}) …")
    if winsetup.remove_from_user_path(install_dir, reg):
        _say("✔ Retiré du PATH")
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
        # Un .exe en cours d'exécution ne peut pas se supprimer lui-même :
        # on confie la suppression du dossier à cmd, quelques secondes après notre sortie.
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["cmd", "/c", f'ping 127.0.0.1 -n 4 > nul & rmdir /s /q "{install_dir}"'],
            creationflags=flags, close_fds=True,
        )
    else:
        shutil.rmtree(install_dir, ignore_errors=True)
    _say("✔ robops a été désinstallé.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_output()
    parser = argparse.ArgumentParser(
        prog="robops-setup",
        description="Installe, met à jour ou désinstalle robops depuis les releases GitHub.",
    )
    parser.add_argument("--uninstall", action="store_true", help="désinstalle robops")
    parser.add_argument("--force", action="store_true", help="réinstalle même si la version est à jour")
    parser.add_argument("--dir", type=Path, help="dossier d'installation (défaut : %%LOCALAPPDATA%%\\Programs\\robops)")
    parser.add_argument("--quiet", action="store_true", help="n'attend pas d'appui sur Entrée à la fin")
    args = parser.parse_args(argv)

    install_dir = (args.dir or winsetup.default_install_dir()).expanduser()
    try:
        if args.uninstall:
            code = uninstall(install_dir)
        else:
            if os.name != "nt":
                _say("⚠ Cet installateur est prévu pour Windows (PATH via le registre utilisateur).")
                return 1
            code = install(install_dir, force=args.force)
    except updater.UpdateError as exc:
        _say(f"✖ {exc}")
        code = 1
    except OSError as exc:
        _say(f"✖ Erreur de fichier : {exc}")
        code = 1

    if not args.quiet and _launched_by_double_click():
        try:
            input("\nAppuie sur Entrée pour fermer cette fenêtre…")
        except EOFError:
            pass
    return code
